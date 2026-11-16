from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection, Row
from typing import TYPE_CHECKING, Iterator
import json
import sqlite3

from .contracts import HarvestError, Manifest, Record, Source, canonical
from .intake import manifest_digest
from .normalize import org_key

if TYPE_CHECKING:
    from .organizations import ResolutionDecl


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS sources (
 key TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
 kind TEXT NOT NULL, field_map TEXT NOT NULL, trust INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS batches (
 source TEXT NOT NULL REFERENCES sources(key), batch TEXT NOT NULL,
 captured_at TEXT NOT NULL, manifest_hash TEXT NOT NULL,
 manifest_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 started_at TEXT NOT NULL, completed_at TEXT,
 PRIMARY KEY(source,batch)
);
CREATE TABLE IF NOT EXISTS pages (
 source TEXT NOT NULL, batch TEXT NOT NULL, ordinal INTEGER NOT NULL,
 path TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, error_detail TEXT,
 processed_at TEXT, record_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(source,batch,ordinal),
 FOREIGN KEY(source,batch) REFERENCES batches(source,batch)
);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, batch TEXT NOT NULL,
 ordinal INTEGER NOT NULL, record_id TEXT NOT NULL, identity TEXT NOT NULL,
 record_json TEXT NOT NULL, raw_json TEXT NOT NULL, raw_digest TEXT NOT NULL,
 status TEXT NOT NULL, reasons TEXT NOT NULL, version INTEGER NOT NULL,
 captured_at TEXT NOT NULL, observed_at TEXT NOT NULL,
 UNIQUE(source,batch,ordinal,record_id),
 FOREIGN KEY(source,batch,ordinal) REFERENCES pages(source,batch,ordinal)
);
CREATE INDEX IF NOT EXISTS observations_identity ON observations(identity);
CREATE INDEX IF NOT EXISTS observations_source_record ON observations(source,record_id,version);
CREATE TABLE IF NOT EXISTS source_records (
 source TEXT NOT NULL REFERENCES sources(key), record_id TEXT NOT NULL,
 version INTEGER NOT NULL, latest_observation INTEGER NOT NULL REFERENCES observations(id),
 PRIMARY KEY(source,record_id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
 source TEXT, batch TEXT, ordinal INTEGER, payload TEXT NOT NULL,
 occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organizations (
 org_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_identifiers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, org_key TEXT NOT NULL REFERENCES organizations(org_key),
 identifier TEXT NOT NULL, source TEXT NOT NULL REFERENCES sources(key),
 batch TEXT NOT NULL, ordinal INTEGER NOT NULL, record_id TEXT NOT NULL,
 first_observed_at TEXT NOT NULL,
 UNIQUE(org_key, identifier, source, batch, ordinal, record_id)
);
CREATE INDEX IF NOT EXISTS organization_identifiers_code ON organization_identifiers(identifier);
CREATE TABLE IF NOT EXISTS organization_resolutions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resolution_id TEXT NOT NULL UNIQUE,
 left_org TEXT NOT NULL REFERENCES organizations(org_key),
 right_org TEXT NOT NULL REFERENCES organizations(org_key),
 basis TEXT NOT NULL, reason TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
 created_at TEXT NOT NULL, revoked_at TEXT, revoke_reason TEXT,
 CHECK(left_org <> right_org), CHECK(status IN ('active','revoked'))
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA busy_timeout=30000")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def event(self, kind: str, source: str | None, batch: str | None,
              ordinal: int | None, payload: dict) -> None:
        self.db.execute("INSERT INTO events(event_type,source,batch,ordinal,payload,occurred_at) VALUES(?,?,?,?,?,?)",
                        (kind, source, batch, ordinal, canonical(payload), utc_now()))

    def register(self, source: Source) -> dict:
        with self.transaction():
            old = self.db.execute("SELECT * FROM sources WHERE key=?", (source.key,)).fetchone()
            if old:
                if old["withdrawn_at"]:
                    raise HarvestError("source_withdrawn", source.key)
                old_document = {"key": old["key"], "name": old["name"], "format": old["format"],
                                "kind": old["kind"], "field_map": json.loads(old["field_map"]),
                                "trust": old["trust"]}
                if old_document == source.document():
                    return {"source": source.key, "revision": old["revision"], "unchanged": True}
                count = self.db.execute("SELECT count(*) FROM batches WHERE source=?", (source.key,)).fetchone()[0]
                if count and (old["format"] != source.format or old["kind"] != source.kind
                              or json.loads(old["field_map"]) != source.field_map):
                    raise HarvestError("source_contract_frozen", "format, kind, and field map cannot change after intake")
                revision = old["revision"] + 1
                self.db.execute("UPDATE sources SET name=?,trust=?,revision=? WHERE key=?",
                                (source.name, source.trust, revision, source.key))
                self.event("source_revised", source.key, None, None, {"revision": revision})
                return {"source": source.key, "revision": revision, "unchanged": False}
            self.db.execute("INSERT INTO sources(key,name,format,kind,field_map,trust) VALUES(?,?,?,?,?,?)",
                            (source.key, source.name, source.format, source.kind,
                             canonical(source.field_map), source.trust))
            self.event("source_registered", source.key, None, None, source.document())
            return {"source": source.key, "revision": 1, "unchanged": False}

    def source(self, key: str) -> Source:
        row = self.db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
        if row is None:
            raise HarvestError("source_unknown", key)
        if row["withdrawn_at"]:
            raise HarvestError("source_withdrawn", key)
        return Source(row["key"], row["name"], row["format"], row["kind"],
                      json.loads(row["field_map"]), row["trust"])

    def ensure_batch(self, manifest: Manifest) -> dict:
        self.source(manifest.source)
        digest = manifest_digest(manifest)
        with self.transaction():
            existing = self.db.execute("SELECT * FROM batches WHERE source=? AND batch=?",
                                       (manifest.source, manifest.batch)).fetchone()
            if existing:
                if existing["manifest_hash"] != digest:
                    raise HarvestError("batch_immutable", "batch identifier already has different manifest")
                return self.batch(manifest.source, manifest.batch)
            self.db.execute("INSERT INTO batches(source,batch,captured_at,manifest_hash,manifest_json,started_at) VALUES(?,?,?,?,?,?)",
                            (manifest.source, manifest.batch, manifest.captured_at, digest,
                             canonical(manifest.document()), utc_now()))
            self.db.executemany("INSERT INTO pages(source,batch,ordinal,path,digest) VALUES(?,?,?,?,?)",
                                [(manifest.source, manifest.batch, p.ordinal, p.path, p.sha256)
                                 for p in manifest.pages])
            self.event("batch_registered", manifest.source, manifest.batch, None,
                       {"manifest_hash": digest, "page_count": len(manifest.pages)})
        return self.batch(manifest.source, manifest.batch)

    def batch(self, source: str, batch: str) -> dict:
        row = self.db.execute("SELECT * FROM batches WHERE source=? AND batch=?", (source, batch)).fetchone()
        if row is None:
            raise HarvestError("batch_unknown", f"{source}/{batch}")
        pages = [dict(p) for p in self.db.execute("SELECT * FROM pages WHERE source=? AND batch=? ORDER BY ordinal",
                                                 (source, batch))]
        return {"source": source, "batch": batch, "captured_at": row["captured_at"],
                "manifest_hash": row["manifest_hash"], "state": row["state"],
                "pages": pages, "completed_at": row["completed_at"]}

    def page(self, source: str, batch: str, ordinal: int) -> dict:
        row = self.db.execute("SELECT * FROM pages WHERE source=? AND batch=? AND ordinal=?",
                              (source, batch, ordinal)).fetchone()
        if row is None:
            raise HarvestError("page_unknown", f"{source}/{batch}/{ordinal}")
        return dict(row)

    def fail_page(self, source: str, batch: str, ordinal: int, error: HarvestError) -> None:
        with self.transaction():
            row = self.page(source, batch, ordinal)
            if row["state"] == "complete":
                return
            self.db.execute("UPDATE pages SET state='failed',attempts=attempts+1,error_code=?,error_detail=? WHERE source=? AND batch=? AND ordinal=?",
                            (error.code, error.detail, source, batch, ordinal))
            self.db.execute("UPDATE batches SET state='failed' WHERE source=? AND batch=?", (source, batch))
            self.event("page_failed", source, batch, ordinal, {"code": error.code, "detail": error.detail})

    def commit_page(self, source: str, batch: str, ordinal: int,
                    items: list[tuple[Record, dict, str, tuple[str, ...]]]) -> dict:
        with self.transaction():
            row = self.page(source, batch, ordinal)
            if row["state"] == "complete":
                return {"ordinal": ordinal, "unchanged": True, "records": row["record_count"]}
            captured = self.db.execute("SELECT captured_at FROM batches WHERE source=? AND batch=?",
                                       (source, batch)).fetchone()[0]
            self.register_organization_claims(
                source, batch, ordinal, [item[0] for item in items])
            for record, raw, status, reasons in items:
                previous = self.db.execute("SELECT version FROM source_records WHERE source=? AND record_id=?",
                                           (source, record.source_record_id)).fetchone()
                version = previous[0] + 1 if previous else 1
                cursor = self.db.execute("INSERT INTO observations(source,batch,ordinal,record_id,identity,record_json,raw_json,raw_digest,status,reasons,version,captured_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                         (source, batch, ordinal, record.source_record_id, record.identity,
                                          canonical(record.document()), canonical(raw), record.raw_digest,
                                          status, canonical(reasons), version, captured, utc_now()))
                self.db.execute("INSERT INTO source_records(source,record_id,version,latest_observation) VALUES(?,?,?,?) ON CONFLICT(source,record_id) DO UPDATE SET version=excluded.version,latest_observation=excluded.latest_observation",
                                (source, record.source_record_id, version, cursor.lastrowid))
            self.db.execute("UPDATE pages SET state='complete',attempts=attempts+1,error_code=NULL,error_detail=NULL,processed_at=?,record_count=? WHERE source=? AND batch=? AND ordinal=?",
                            (utc_now(), len(items), source, batch, ordinal))
            self.event("page_committed", source, batch, ordinal, {"records": len(items)})
            incomplete = self.db.execute("SELECT count(*) FROM pages WHERE source=? AND batch=? AND state!='complete'",
                                         (source, batch)).fetchone()[0]
            if incomplete == 0:
                self.db.execute("UPDATE batches SET state='complete',completed_at=? WHERE source=? AND batch=?",
                                (utc_now(), source, batch))
                self.event("batch_completed", source, batch, None, {"records_last_page": len(items)})
            else:
                failed = self.db.execute("SELECT count(*) FROM pages WHERE source=? AND batch=? AND state='failed'",
                                         (source, batch)).fetchone()[0]
                self.db.execute("UPDATE batches SET state=? WHERE source=? AND batch=?",
                                ("failed" if failed else "pending", source, batch))
            return {"ordinal": ordinal, "unchanged": False, "records": len(items)}

    def withdraw(self, source: str, reason: str) -> dict:
        with self.transaction():
            row = self.db.execute("SELECT withdrawn_at FROM sources WHERE key=?", (source,)).fetchone()
            if row is None:
                raise HarvestError("source_unknown", source)
            if row[0]:
                return {"source": source, "withdrawn_at": row[0], "unchanged": True}
            at = utc_now()
            self.db.execute("UPDATE sources SET withdrawn_at=? WHERE key=?", (at, source))
            self.event("source_withdrawn", source, None, None, {"reason": reason})
            return {"source": source, "withdrawn_at": at, "unchanged": False}

    def register_organization_claims(self, source: str, batch: str, ordinal: int,
                                     records: list[Record]) -> None:
        """在页事务内登记机构与来源自报标识；标识与机构分开保存，不触碰原始记录。

        first_seen_at/last_seen_at 一律使用本地入库时钟，避免与来源提供的
        captured_at 混用时间基；批次捕获时间仍完整保留在观察记录中。
        """
        at = utc_now()
        for record in records:
            key = org_key(record.organization)
            self.db.execute(
                "INSERT INTO organizations(org_key,display_name,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?) ON CONFLICT(org_key) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (key, record.organization, at, at))
            if record.organization_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO organization_identifiers"
                    "(org_key,identifier,source,batch,ordinal,record_id,first_observed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (key, record.organization_id, source, batch, ordinal,
                     record.source_record_id, at))

    def organization(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM organizations WHERE org_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def organization_identifiers(self, *, include_withdrawn: bool = False) -> list[dict]:
        sql = ("SELECT i.*, s.withdrawn_at FROM organization_identifiers i "
               "JOIN sources s ON s.key=i.source")
        if not include_withdrawn:
            sql += " WHERE s.withdrawn_at IS NULL"
        sql += " ORDER BY i.identifier,i.org_key,i.source"
        return [dict(row) for row in self.db.execute(sql).fetchall()]

    def active_resolutions(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM organization_resolutions WHERE status='active' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def resolutions(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM organization_resolutions ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def confirm_resolution(self, declaration: "ResolutionDecl") -> dict:
        left, right = org_key(declaration.left_org), org_key(declaration.right_org)
        if left == right:
            raise HarvestError("resolution_same_org", "a resolution must join two distinct organizations")
        with self.transaction():
            if not self.organization(left) or not self.organization(right):
                raise HarvestError("resolution_org_unknown",
                                   "both organizations must be observed before a manual confirmation")
            existing = self.db.execute(
                "SELECT * FROM organization_resolutions WHERE left_org=? AND right_org=? "
                "OR left_org=? AND right_org=?", (left, right, right, left)).fetchall()
            active = [row for row in existing if row["status"] == "active"]
            if active:
                row = active[0]
                return {"resolution_id": row["resolution_id"], "revision": row["revision"],
                        "status": row["status"], "unchanged": True}
            # 已通过其他生效决议传递连通：机构边界本就相同，登记为无操作幂等返回。
            if self._already_connected(left, right):
                return {"resolution_id": None, "revision": None, "status": "active",
                        "unchanged": True, "transitive": True}
            at = utc_now()
            revision = max((row["revision"] for row in existing), default=0) + 1
            # 全局序列保证 resolution_id 唯一，不依赖可能碰撞的机构名前缀。
            sequence = self.db.execute(
                "SELECT coalesce(max(id),0) + 1 FROM organization_resolutions").fetchone()[0]
            resolution_id = f"res-{sequence:08d}"
            cursor = self.db.execute(
                "INSERT INTO organization_resolutions"
                "(resolution_id,left_org,right_org,basis,reason,revision,status,created_at) "
                "VALUES(?,?,?,?,?,?, 'active',?)",
                (resolution_id, left, right, declaration.basis,
                 declaration.reason, revision, at))
            payload = {"resolution_id": resolution_id, "left_org": left, "right_org": right,
                       "basis": declaration.basis, "reason": declaration.reason, "revision": revision}
            self.event("org_resolution_confirmed", None, None, None, payload)
            row = self.db.execute("SELECT * FROM organization_resolutions WHERE id=?",
                                  (cursor.lastrowid,)).fetchone()
            return {"resolution_id": resolution_id, "revision": row["revision"],
                    "status": "active", "unchanged": False}

    def _already_connected(self, left: str, right: str) -> bool:
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for row in self.active_resolutions():
            ra, rb = find(row["left_org"]), find(row["right_org"])
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
        return find(left) == find(right)

    def revoke_resolution(self, resolution_id: str, reason: str) -> dict:
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM organization_resolutions WHERE resolution_id=?",
                (resolution_id,)).fetchone()
            if row is None:
                raise HarvestError("resolution_unknown", resolution_id)
            if row["status"] == "revoked":
                return {"resolution_id": resolution_id, "status": "revoked",
                        "revoked_at": row["revoked_at"], "unchanged": True}
            at = utc_now()
            self.db.execute(
                "UPDATE organization_resolutions SET status='revoked',revoked_at=?,revoke_reason=? WHERE id=?",
                (at, reason, row["id"]))
            self.event("org_resolution_revoked", None, None, None,
                       {"resolution_id": resolution_id, "left_org": row["left_org"],
                        "right_org": row["right_org"], "reason": reason})
            return {"resolution_id": resolution_id, "status": "revoked",
                    "revoked_at": at, "unchanged": False}

    def observations(self, *, source: str | None = None, identity: str | None = None) -> list[dict]:
        conditions = []
        params = []
        if source is not None:
            conditions.append("o.source=?")
            params.append(source)
        if identity is not None:
            conditions.append("o.identity=?")
            params.append(identity)
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.execute("SELECT o.*,s.trust,s.withdrawn_at FROM observations o JOIN sources s ON s.key=o.source"
                               + clause + " ORDER BY o.id", params)
        return [{**dict(row), "record": json.loads(row["record_json"]),
                 "raw": json.loads(row["raw_json"]), "reasons": json.loads(row["reasons"])} for row in rows]

    def events(self, *, source: str | None = None) -> list[dict]:
        if source is None:
            rows = self.db.execute("SELECT * FROM events ORDER BY id")
        else:
            rows = self.db.execute("SELECT * FROM events WHERE source=? ORDER BY id", (source,))
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
