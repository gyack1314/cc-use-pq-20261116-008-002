"""机构边界解析：来源自报标识、人工确认决议与机构分组。

设计原则
--------
- 镜像里的机构标识（如统一社会信用代码）只是来源的*声明*，与机构实体分开保存，
  采集时绝不据此改写原始机构名或实体键。
- 两个机构是否同一主体，只能由人工确认（organization_resolutions）决定；
  系统不做模糊别名推断。
- 人工确认是有版本、有状态、有审计事件的独立决议；撤销后机构边界恢复原样。
- 后续镜像若给出与生效人工确认冲突的标识，相关页在提交前被识别并隔离。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import HarvestError, required
from .ledger import Ledger
from .normalize import org_key


@dataclass(frozen=True)
class ResolutionDecl:
    """一次人工确认：left_org 与 right_org 属于同一机构。"""
    left_org: str
    right_org: str
    basis: str
    reason: str

    @classmethod
    def parse(cls, value: Any) -> "ResolutionDecl":
        if not isinstance(value, dict):
            raise HarvestError("resolution_shape", "resolution must be an object")
        left = required(value.get("left_org"), "left_org", 240)
        right = required(value.get("right_org"), "right_org", 240)
        basis = required(value.get("basis", "manual"), "basis", 120)
        reason = required(value.get("reason"), "reason", 500)
        if org_key(left) == org_key(right):
            raise HarvestError("resolution_same_org",
                               "a resolution must join two distinct organizations")
        return cls(left, right, basis, reason)

    def document(self) -> dict[str, Any]:
        return {"left_org": self.left_org, "right_org": self.right_org,
                "basis": self.basis, "reason": self.reason}


class OrganizationRegistry:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    # ------------------------------------------------------------------
    # 机构分组（并查集，仅由生效中的人工确认驱动）
    # ------------------------------------------------------------------
    def group_map(self) -> dict[str, str]:
        """返回 org_key -> 组根键。未被任何确认覆盖的机构自成一组。"""
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                # 取字典序最小者为根，保证分组结果稳定可复现。
                if root_a < root_b:
                    parent[root_b] = root_a
                else:
                    parent[root_a] = root_b

        for row in self.ledger.db.execute("SELECT org_key FROM organizations"):
            parent[row["org_key"]] = row["org_key"]
        for resolution in self.ledger.active_resolutions():
            union(resolution["left_org"], resolution["right_org"])
        return {key: find(key) for key in parent}

    def merged_pairs(self) -> list[dict]:
        """当前生效人工确认合并了哪些机构对（含展示名）。"""
        names = {row["org_key"]: row["display_name"]
                 for row in self.ledger.db.execute("SELECT org_key,display_name FROM organizations")}
        result = []
        for resolution in self.ledger.active_resolutions():
            result.append({"resolution_id": resolution["resolution_id"],
                           "revision": resolution["revision"],
                           "left_org": resolution["left_org"],
                           "left_name": names.get(resolution["left_org"], resolution["left_org"]),
                           "right_org": resolution["right_org"],
                           "right_name": names.get(resolution["right_org"], resolution["right_org"]),
                           "basis": resolution["basis"], "reason": resolution["reason"]})
        return result

    # ------------------------------------------------------------------
    # 与人工确认冲突的机构标识（提交前拦截）
    # ------------------------------------------------------------------
    def find_contradictions(self, records: list) -> list[dict]:
        """检查待提交页：机构标识是否与*生效人工确认*冲突，冲突则整页隔离。

        判定同时纳入已持久化声明与本页待提交声明，避免同页互斥声明漏检。
        两类矛盾（均以存在生效人工确认为前提，否则只是待决分歧、不阻塞）：
        1. 同一标识被声明给处于不同决议组的机构——人工确认说它们是不同主体，
           镜像却给了相同信用代码。
        2. 同一决议组内两个机构各自持有对方没有的标识（双向都存在独有码）——
           人工确认说它们是同一主体，镜像却让两边出现互不认可的信用代码。
           仅一方累积新码、另一方的码仍被完全覆盖时属于正常的登记号补充，不判冲突。
        """
        if not self.ledger.active_resolutions():
            return []
        groups = self.group_map()
        # 标识 -> 持有该标识的机构键集合；机构键 -> 持有标识集合。
        # 先装入已持久化的有效声明，再并入本页待提交声明（按机构求并集，避免覆盖）。
        owners: dict[str, set[str]] = {}
        codes_by_org: dict[str, set[str]] = {}
        for claim in self.ledger.organization_identifiers():
            owners.setdefault(claim["identifier"], set()).add(claim["org_key"])
            codes_by_org.setdefault(claim["org_key"], set()).add(claim["identifier"])
        for record in records:
            if record.organization_id:
                key = org_key(record.organization)
                owners.setdefault(record.organization_id, set()).add(key)
                codes_by_org.setdefault(key, set()).add(record.organization_id)

        findings: list[dict] = []
        seen: set[tuple] = set()
        org_pairs = sorted(codes_by_org)

        def emit(record, kind, other_org, detail) -> None:
            fingerprint = (record.source_record_id, kind, other_org)
            if fingerprint in seen:
                return
            seen.add(fingerprint)
            findings.append({"record_id": record.source_record_id,
                             "organization": record.organization,
                             "org_key": org_key(record.organization),
                             "identifier": record.organization_id,
                             "kind": kind,
                             "group": groups.get(org_key(record.organization),
                                                 org_key(record.organization)),
                             "conflicting_org": other_org, **detail})

        # 先在合并后的全局集合上计算所有矛盾机构对（含本页新声明），
        # 再归因到页内记录，避免同页互斥声明因互不可见而漏检。
        cross_group_shared: set[frozenset[str]] = set()
        for identifier, holder_orgs in owners.items():
            for org_a in holder_orgs:
                for org_b in holder_orgs:
                    if org_a < org_b and groups.get(org_a, org_a) != groups.get(org_b, org_b):
                        cross_group_shared.add(frozenset((org_a, org_b)))
        divergent_same_group: set[frozenset[str]] = set()
        for index, org_a in enumerate(org_pairs):
            codes_a = codes_by_org[org_a]
            for org_b in org_pairs[index + 1:]:
                if groups.get(org_a, org_a) != groups.get(org_b, org_b):
                    continue
                codes_b = codes_by_org[org_b]
                # 双向各有对方没有的标识：同一机构组内出现互不认可的信用代码
                if (codes_a - codes_b) and (codes_b - codes_a):
                    divergent_same_group.add(frozenset((org_a, org_b)))

        for record in records:
            if not record.organization_id:
                continue
            claim_org = org_key(record.organization)
            for pair in cross_group_shared:
                if claim_org in pair:
                    other_org = next(iter(pair - {claim_org}))
                    emit(record, "shared_identifier_across_resolution_groups", other_org,
                         {"other_group": groups.get(other_org, other_org)})
            for pair in divergent_same_group:
                if claim_org in pair:
                    other_org = next(iter(pair - {claim_org}))
                    emit(record, "divergent_identifiers_within_resolution_group", other_org,
                         {"own_codes": sorted(codes_by_org[claim_org]),
                          "other_codes": sorted(codes_by_org[other_org])})
        return findings

    # ------------------------------------------------------------------
    # 待决标识分歧（尚未经人工确认，不阻塞，但需提示）
    # ------------------------------------------------------------------
    def identifier_disputes(self) -> list[dict]:
        """同一标识被多个机构组持有时列出；撤回来源的声明保留并标注，不参与当前交付。"""
        groups = self.group_map()
        withdrawn = {row["key"] for row in self.ledger.db.execute(
            "SELECT key FROM sources WHERE withdrawn_at IS NOT NULL").fetchall()}
        claims: dict[str, dict[str, set[str]]] = {}
        for claim in self.ledger.organization_identifiers(include_withdrawn=True):
            entry = claims.setdefault(claim["identifier"], {})
            entry.setdefault(claim["org_key"], set()).add(claim["source"])
        disputes = []
        for identifier, by_org in sorted(claims.items()):
            group_set = {groups.get(key, key) for key in by_org}
            if len(by_org) < 2 or len(group_set) < 2:
                continue
            organizations = []
            for key, sources in sorted(by_org.items()):
                source_list = sorted(sources)
                organizations.append({"org_key": key, "sources": source_list,
                                      "group": groups.get(key, key),
                                      "withdrawn_sources": sorted(s for s in source_list
                                                                  if s in withdrawn)})
            disputes.append({
                "identifier": identifier,
                "status": "pending_confirmation",
                "organizations": organizations,
            })
        return disputes

    def organizations(self) -> list[dict]:
        groups = self.group_map()
        rows = self.ledger.db.execute(
            "SELECT o.org_key, o.display_name, o.first_seen_at, o.last_seen_at, "
            "count(i.id) AS active_identifier_claims "
            "FROM organizations o "
            "LEFT JOIN organization_identifiers i ON i.org_key = o.org_key "
            "LEFT JOIN sources s ON s.key = i.source AND s.withdrawn_at IS NULL "
            "GROUP BY o.org_key ORDER BY o.org_key").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["group_key"] = groups.get(row["org_key"], row["org_key"])
            result.append(item)
        return result

    def confirm(self, declaration: Any) -> dict:
        return self.ledger.confirm_resolution(
            declaration if isinstance(declaration, ResolutionDecl)
            else ResolutionDecl.parse(declaration))

    def revoke(self, resolution_id: str, reason: str) -> dict:
        if not reason or not reason.strip():
            raise HarvestError("resolution_reason", "revoking a confirmation requires a reason")
        return self.ledger.revoke_resolution(resolution_id, reason.strip())
