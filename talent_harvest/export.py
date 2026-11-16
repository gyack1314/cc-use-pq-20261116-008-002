from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
import json

from .contracts import HarvestError, canonical
from .ledger import Ledger
from .normalize import org_key, search_key
from .organizations import OrganizationRegistry
from .reconcile import reconcile_group


def _latest_per_source(rows: list[dict]) -> list[dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = row["source"], row["record_id"]
        previous = latest.get(key)
        if previous is None or row["id"] > previous["id"]:
            latest[key] = row
    return list(latest.values())


def _project(row: dict) -> dict:
    return {"source": row["source"], "source_record_id": row["record_id"],
            "batch": row["batch"], "page": row["ordinal"], "version": row["version"],
            "captured_at": row["captured_at"], "observed_at": row["observed_at"],
            "status": row["status"], "reasons": row["reasons"], "trust": row["trust"],
            "record": row["record"], "raw_digest": row["raw_digest"]}


def merged_entity_key(row: dict, groups: dict[str, str]) -> str:
    """人工确认后的实体键：以机构组根替换原始机构段，其余维度保持不变。"""
    record = row["record"]
    group = groups.get(org_key(record["organization"]), org_key(record["organization"]))
    material = "|".join((record["kind"], group,
                         search_key(record["title"]), search_key(record["region"])))
    return sha256(material.encode("utf-8")).hexdigest()


class Delivery:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def current(self, *, include_quarantine: bool = False) -> dict:
        rows = [row for row in _latest_per_source(self.ledger.observations()) if not row["withdrawn_at"]]
        registry = OrganizationRegistry(self.ledger)
        groups = registry.group_map()
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[merged_entity_key(row, groups)].append(row)
        accepted: list[dict] = []
        conflicts: list[dict] = []
        quarantined: list[dict] = []
        for entity_key in sorted(grouped):
            group = grouped[entity_key]
            rejected = [row for row in group if row["status"] != "accepted"]
            if include_quarantine:
                quarantined.extend(_project(row) for row in rejected)
            status, reason, winner = reconcile_group(group)
            organizations = sorted({row["record"]["organization"] for row in group})
            org_keys = sorted({org_key(row["record"]["organization"]) for row in group})
            identities = sorted({row["identity"] for row in group})
            if status == "accepted" and winner:
                accepted.append({"entity_key": entity_key,
                                 "identity": identities[0],
                                 "identities": identities,
                                 "organizations": organizations, "org_keys": org_keys,
                                 "record": winner["record"],
                                 "selected": _project(winner),
                                 "evidence": [_project(item) for item in sorted(group, key=lambda x: (x["source"], x["record_id"]))
                                              if item["status"] == "accepted"]})
            elif status == "conflict":
                conflicts.append({"entity_key": entity_key,
                                  "identity": identities[0],
                                  "identities": identities,
                                  "organizations": organizations, "org_keys": org_keys,
                                  "reason": reason,
                                  "evidence": [_project(item) for item in group if item["status"] == "accepted"]})
        registry_disputes = registry.identifier_disputes()
        return {"schema_version": 1, "accepted": accepted, "conflicts": conflicts,
                "quarantined": quarantined if include_quarantine else [],
                "organization_resolutions": registry.merged_pairs(),
                "org_identifier_disputes": registry_disputes,
                "counts": {"accepted": len(accepted), "conflicts": len(conflicts),
                           "quarantined": len(quarantined) if include_quarantine else 0}}

    def history(self, *, source: str | None = None, identity: str | None = None) -> dict:
        rows = self.ledger.observations(source=source, identity=identity)
        return {"schema_version": 1, "observations":
                [{**_project(row), "identity": row["identity"], "raw": row["raw"],
                  "withdrawn_at": row["withdrawn_at"]} for row in rows]}

    def trace(self, identity: str) -> dict:
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise HarvestError("identity", "identity must be sha256 hex")
        history = self.history(identity=identity)["observations"]
        if not history:
            raise HarvestError("identity_unknown", identity)
        sources = sorted({row["source"] for row in history})
        registry = OrganizationRegistry(self.ledger)
        events = [event for event in self.ledger.events()
                  if event["source"] in sources
                  or (event["event_type"].startswith("org_resolution")
                      and self._event_touches_history(event, history, registry))]
        current = self.current(include_quarantine=True)
        matching = [item for section in ("accepted", "conflicts") for item in current[section]
                    if identity in item["identities"]]
        return {"identity": identity, "history": history, "events": events,
                "current": matching[0] if matching else None}

    @staticmethod
    def _event_touches_history(event: dict, history: list[dict],
                               registry: OrganizationRegistry) -> bool:
        payload = event.get("payload", {})
        touched = {payload.get("left_org"), payload.get("right_org")}
        history_orgs = {org_key(row["record"]["organization"]) for row in history}
        return bool(touched & history_orgs)

    def summary(self) -> dict:
        current = self.current(include_quarantine=True)
        by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"entities": 0, "quantity": 0})
        by_region: dict[str, dict[str, int]] = defaultdict(lambda: {"entities": 0, "quantity": 0})
        for entity in current["accepted"]:
            record = entity["record"]
            by_kind[record["kind"]]["entities"] += 1
            by_kind[record["kind"]]["quantity"] += record["quantity"]
            by_region[record["region"]]["entities"] += 1
            by_region[record["region"]]["quantity"] += record["quantity"]
        return {"counts": current["counts"], "by_kind": dict(sorted(by_kind.items())),
                "by_region": dict(sorted(by_region.items()))}

    def write_snapshot(self, destination: str | Path, *, include_quarantine: bool = False) -> dict:
        path = Path(destination)
        payload = self.current(include_quarantine=include_quarantine)
        data = canonical(payload).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)
        return {"path": str(path), "sha256": sha256(data).hexdigest(),
                "bytes": len(data), "counts": payload["counts"]}


def verify_snapshot(path: str | Path, expected_sha256: str) -> dict:
    payload = Path(path).read_bytes()
    actual = sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise HarvestError("snapshot_digest", f"expected {expected_sha256}, got {actual}")
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        raise HarvestError("snapshot_json", "snapshot is not valid UTF-8 JSON") from None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise HarvestError("snapshot_schema", "unexpected delivery schema")
    return {"sha256": actual, "counts": data.get("counts", {})}
