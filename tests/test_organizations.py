from __future__ import annotations

import json
import unittest

from talent_harvest.audit import Auditor
from talent_harvest.contracts import HarvestError
from talent_harvest.export import Delivery
from talent_harvest.inventory import Inventory
from talent_harvest.organizations import OrganizationRegistry, ResolutionDecl
from talent_harvest.ops import Operations
from tests.support import Workspace, record, source

CODE_A = "91310000MA1FL8XQ4A"
CODE_B = "91110000MA01ABCD2X"


class OrganizationIdentifierTests(unittest.TestCase):
    def setUp(self):
        self.work = Workspace()
        self.work.register(key="alpha", trust=70)
        self.work.register(key="beta", trust=80)

    def tearDown(self):
        self.work.close()

    def test_identifier_is_stored_separately_and_never_rewrites_org(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        observation = self.work.ledger.observations(source="alpha")[0]
        # 原始机构名原样保留，标识作为独立字段保存
        self.assertEqual(observation["record"]["organization"], "FT")
        self.assertEqual(observation["record"]["organization_id"], CODE_A)
        # 标识不参与实体身份键：带码与不带码的同机构同岗位身份一致
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="FT", title="AI Engineer",
                   region="Shanghai")]])
        identities = {row["identity"] for row in self.work.ledger.observations()}
        self.assertEqual(len(identities), 1)
        claim = self.work.ledger.organization_identifiers()[0]
        self.assertEqual(claim["identifier"], CODE_A)
        self.assertEqual(claim["org_key"], "ft")

    def test_invalid_identifier_shape_is_rejected_at_page_boundary(self):
        # 形态非法的字符串标识使整页失败，错误保存在页上且不写入观察记录
        cases = (("short", "ab"), ("space", "has space"), ("punct", "abc-123-456"))
        for suffix, bad in cases:
            with self.subTest(bad=bad):
                _, result = self.work.ingest(
                    key="alpha", batch=f"bad-{suffix}",
                    pages=[[record(record_id=f"x-{suffix}", organization_id=bad)]])
                self.assertEqual(result["state"], "failed")
                self.assertEqual(
                    self.work.ledger.page("alpha", f"bad-{suffix}", 1)["error_code"],
                    "organization_id")
        # 非字符串字段同样在页边界被拒；organization_identifier 的类型拒绝另由契约层校验
        _, result = self.work.ingest(
            key="alpha", batch="bad-int",
            pages=[[{**record(record_id="y"), "organization_id": True}]])
        self.assertEqual(result["state"], "failed")
        self.assertIsNotNone(self.work.ledger.page("alpha", "bad-int", 1)["error_code"])
        self.assertEqual(len(self.work.ledger.observations(source="alpha")), 0)

    def test_unconfirmed_alias_keeps_two_entities_and_pending_dispute(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        view = Delivery(self.work.ledger).current()
        # 未人工确认：两个机构边界独立，系统不做模糊别名合并
        self.assertEqual(view["counts"]["accepted"], 2)
        # 同标识跨机构属于待决分歧，可采入但不阻塞
        disputes = view["org_identifier_disputes"]
        self.assertEqual([d["identifier"] for d in disputes], [CODE_A])
        self.assertEqual(disputes[0]["status"], "pending_confirmation")

    def test_manual_confirmation_merges_entity_boundary(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", title="AI Engineer",
                   region="Shanghai", quantity=3, organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", title="AI Engineer",
                   region="Shanghai", quantity=3, organization_id=CODE_A)]])
        result = self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech",
             "basis": "service_window", "reason": "窗口人工核对同一机构"})
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["revision"], 1)
        view = Delivery(self.work.ledger).current()
        self.assertEqual(view["counts"]["accepted"], 1)
        entity = view["accepted"][0]
        self.assertEqual(sorted(entity["organizations"]), ["FT", "Fictional Tech"])
        # 原始证据各自保留，实体键与原始身份都可追溯
        self.assertEqual(len(entity["evidence"]), 2)
        self.assertEqual(len(entity["identities"]), 2)
        self.assertTrue(Auditor(self.work.ledger).verify_state()["ok"])

    def test_opposite_quantities_go_to_conflict_not_summed(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", title="AI Engineer",
                   region="Shanghai", quantity=5, organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", title="AI Engineer",
                   region="Shanghai", quantity=2, organization_id=CODE_A)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        view = Delivery(self.work.ledger).current()
        self.assertEqual(view["counts"]["accepted"], 0)
        self.assertEqual(view["counts"]["conflicts"], 1)
        self.assertEqual(view["conflicts"][0]["reason"], "quantity_mismatch")
        # 数量绝不相加：争议实体保留两条独立证据
        self.assertEqual(len(view["conflicts"][0]["evidence"]), 2)

    def test_revoking_confirmation_restores_two_entities_and_evidence(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", title="AI Engineer",
                   region="Shanghai", quantity=3, organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", title="AI Engineer",
                   region="Shanghai", quantity=3, organization_id=CODE_A)]])
        resolution = self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["accepted"], 1)
        revoked = self.work.harvester.revoke_organization(
            resolution["resolution_id"], "确认材料作废")
        self.assertEqual(revoked["status"], "revoked")
        view = Delivery(self.work.ledger).current()
        self.assertEqual(view["counts"]["accepted"], 2)
        self.assertEqual(view["organization_resolutions"], [])
        # 原始证据完整保留
        self.assertEqual(len(self.work.ledger.observations()), 2)
        # 历史决议仍可审计
        history = self.work.ledger.resolutions()
        self.assertEqual([row["status"] for row in history], ["revoked"])

    def test_reconfirm_after_revoke_increments_revision(self):
        self.work.ingest(key="alpha", pages=[[record(record_id="a1", organization="FT")]])
        self.work.ingest(key="beta", pages=[[record(record_id="b1", organization="Fictional Tech")]])
        first = self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "1"})
        self.work.harvester.revoke_organization(first["resolution_id"], "作废")
        second = self.work.harvester.confirm_organization(
            {"left_org": "Fictional Tech", "right_org": "FT", "basis": "manual", "reason": "2"})
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["status"], "active")
        self.assertFalse(second["unchanged"])
        self.assertEqual(len(self.work.ledger.resolutions()), 2)

    def test_duplicate_confirmation_is_idempotent(self):
        self.work.ingest(key="alpha", pages=[[record(record_id="a1", organization="FT")]])
        self.work.ingest(key="beta", pages=[[record(record_id="b1", organization="Fictional Tech")]])
        declaration = {"left_org": "FT", "right_org": "Fictional Tech",
                       "basis": "manual", "reason": "x"}
        first = self.work.harvester.confirm_organization(declaration)
        again = self.work.harvester.confirm_organization(declaration)
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["resolution_id"], first["resolution_id"])
        self.assertEqual(len(self.work.ledger.resolutions()), 1)

    def test_confirmation_requires_observed_organizations(self):
        with self.assertRaises(HarvestError) as caught:
            self.work.harvester.confirm_organization(
                {"left_org": "FT", "right_org": "Unknown Body", "basis": "manual", "reason": "x"})
        self.assertEqual(caught.exception.code, "resolution_org_unknown")

    def test_identifier_conflicting_with_resolution_isolates_page(self):
        self.work.register(key="gamma", trust=60)
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.ingest(key="gamma", pages=[[
            record(record_id="g1", organization="Gamma Labs", organization_id=CODE_B)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        # 同组机构声明另一决议组的标识 -> 整页隔离
        _, blocked = self.work.ingest(
            key="beta", batch="batch-2",
            pages=[[record(record_id="b2", organization="Fictional Tech",
                           organization_id=CODE_B)]])
        self.assertEqual(blocked["state"], "failed")
        page = self.work.ledger.page("beta", "batch-2", 1)
        self.assertEqual(page["error_code"], "organization_identifier_conflict")
        # 旧决议与既有交付不被污染，冲突页不写入观察记录
        self.assertEqual(len(self.work.ledger.observations(source="beta")), 1)
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["accepted"], 2)

    def test_conflicting_page_stays_isolated_on_retry(self):
        self.work.register(key="gamma", trust=60)
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.ingest(key="gamma", pages=[[
            record(record_id="g1", organization="Gamma Labs", organization_id=CODE_B)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        path, first = self.work.ingest(
            key="beta", batch="batch-2",
            pages=[[record(record_id="b2", organization="Fictional Tech",
                           organization_id=CODE_B)]])
        self.assertEqual(first["state"], "failed")
        second = self.work.harvester.resume(path)
        self.assertEqual(second["state"], "failed")
        self.assertEqual(self.work.ledger.page("beta", "batch-2", 1)["attempts"], 2)
        # 恢复计划把隔离页列为可重试，撤销决议后即可解除
        plan = Inventory(self.work.ledger).recovery_plan()
        self.assertEqual(plan["retryable"], 1)

    def test_revoking_resolution_unblocks_conflicting_page(self):
        self.work.register(key="gamma", trust=60)
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.ingest(key="gamma", pages=[[
            record(record_id="g1", organization="Gamma Labs", organization_id=CODE_B)]])
        resolution = self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        self.work.ingest(key="beta", batch="batch-2",
                         pages=[[record(record_id="b2", organization="Fictional Tech",
                                        organization_id=CODE_B)]])
        self.assertEqual(self.work.ledger.page("beta", "batch-2", 1)["state"], "failed")
        self.work.harvester.revoke_organization(resolution["resolution_id"], "作废")
        # 撤销后无生效决议，新批次的相同内容降级为待决分歧，可正常采入
        _, recovered = self.work.ingest(
            key="beta", batch="batch-3",
            pages=[[record(record_id="b2", organization="Fictional Tech",
                           organization_id=CODE_B)]])
        self.assertEqual(recovered["state"], "complete")
        disputes = OrganizationRegistry(self.work.ledger).identifier_disputes()
        self.assertIn(CODE_B, [d["identifier"] for d in disputes])

    def test_withdrawn_source_claims_leave_current_but_keep_history(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.assertEqual(Delivery(self.work.ledger).current()["counts"]["accepted"], 2)
        self.work.harvester.withdraw("beta", "来源停止服务")
        view = Delivery(self.work.ledger).current(include_quarantine=True)
        self.assertEqual(view["counts"]["accepted"], 1)
        # 历史观察与标识声明仍保留，分歧记录标注撤回来源
        self.assertEqual(len(self.work.ledger.observations(source="beta")), 1)
        disputes = OrganizationRegistry(self.work.ledger).identifier_disputes()
        withdrawn = {src for org in disputes[0]["organizations"]
                     for src in org["withdrawn_sources"]}
        self.assertEqual(withdrawn, {"beta"})

    def test_withdrawn_source_exits_contradiction_detection(self):
        self.work.register(key="gamma", trust=60)
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.ingest(key="gamma", pages=[[
            record(record_id="g1", organization="Gamma Labs", organization_id=CODE_B)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        # gamma 在独立组持有 CODE_B；FT 声明 CODE_B 触发跨组冲突并隔离
        _, blocked = self.work.ingest(
            key="alpha", batch="b2",
            pages=[[record(record_id="a2", organization="FT", organization_id=CODE_B)]])
        self.assertEqual(blocked["state"], "failed")
        # 撤回 gamma 后，其 CODE_B 声明退出有效集合，新批次不再被阻塞
        self.work.harvester.withdraw("gamma", "停服")
        active = OrganizationRegistry(self.work.ledger).ledger.organization_identifiers()
        self.assertNotIn("gamma", {claim["org_key"] for claim in active})
        _, recovered = self.work.ingest(
            key="alpha", batch="b3",
            pages=[[record(record_id="a3", organization="FT", organization_id=CODE_B)]])
        self.assertEqual(recovered["state"], "complete")

    def test_cross_batch_claims_accumulate_and_group_consistently(self):
        # 同一来源跨三个批次持续声明同一标识，机构边界始终稳定；数量保持一致避免争议
        for batch in ("b1", "b2", "b3"):
            self.work.ingest(
                key="alpha", batch=batch,
                pages=[[record(record_id="a1", organization="FT", quantity=5,
                               organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", quantity=5,
                   organization_id=CODE_A)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        view = Delivery(self.work.ledger).current()
        self.assertEqual(view["counts"]["accepted"], 1)
        # 首选按信任度选出（beta 80 > alpha 70），数量保持一致不取反、不累加
        selected = view["accepted"][0]["selected"]
        self.assertEqual(selected["source"], "beta")
        self.assertEqual(selected["record"]["quantity"], 5)
        # alpha 自身的记录版本随三个批次演进到 3
        alpha_history = Delivery(self.work.ledger).history(source="alpha")["observations"]
        self.assertEqual([row["version"] for row in alpha_history], [1, 2, 3])
        # 标识声明跨批次完整留存，可追溯每一次来源声明
        claims = [c for c in self.work.ledger.organization_identifiers() if c["org_key"] == "ft"]
        self.assertEqual(len(claims), 3)
        self.assertEqual({c["batch"] for c in claims}, {"b1", "b2", "b3"})

    def test_consistency_report_reflects_merged_boundary(self):
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", title="AI Engineer",
                   region="Shanghai", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", title="AI Engineer",
                   region="Shanghai", organization_id=CODE_A)]])
        before = Operations(self.work.ledger).consistency_report()
        self.assertEqual(len(before["entities"]), 2)
        self.assertEqual(before["merged_organizations"], 0)
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        after = Operations(self.work.ledger).consistency_report()
        self.assertEqual(len(after["entities"]), 1)
        self.assertEqual(after["merged_organizations"], 1)
        self.assertEqual(after["entities"][0]["organizations"], ["FT", "Fictional Tech"])

    def test_resolution_declaration_validation(self):
        with self.assertRaises(HarvestError):
            ResolutionDecl.parse({"left_org": "FT", "right_org": "FT",
                                  "basis": "x", "reason": "y"})
        with self.assertRaises(HarvestError):
            ResolutionDecl.parse({"left_org": "FT", "right_org": "Other",
                                  "basis": "x", "reason": ""})

    def test_preview_flags_conflict_without_mutating_state(self):
        self.work.register(key="gamma", trust=60)
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.ingest(key="gamma", pages=[[
            record(record_id="g1", organization="Gamma Labs", organization_id=CODE_B)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        from tests.support import manifest
        path = manifest(self.work.path, key="beta", batch="preview-1",
                        pages=[[record(record_id="b2", organization="Fictional Tech",
                                       organization_id=CODE_B)]])
        declaration = self.work.path / "beta.json"
        declaration.write_text(json.dumps(source(key="beta")), encoding="utf-8")
        preview = Operations(self.work.ledger).preview(declaration, path)
        self.assertEqual(preview["invalid_pages"], 1)
        self.assertEqual(preview["pages"][0]["code"], "organization_identifier_conflict")
        # 预览不产生任何观察记录
        self.assertEqual(len(self.work.ledger.observations(source="beta")), 1)

    def test_same_page_mutually_exclusive_identifiers_are_isolated(self):
        # 同组机构在同一新页内给出互不相交的标识集合，即使历史无相关声明也必须整页隔离
        code_c = "91110000MA1FL8XQ4C"
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        # 页内 FT 改持全新 CODE_C、Fictional Tech 持 CODE_B，两者集合互不相交
        _, blocked = self.work.ingest(
            key="alpha", batch="same-page",
            pages=[[record(record_id="a2", organization="FT", title="X",
                           organization_id=code_c),
                    record(record_id="a3", organization="Fictional Tech", title="Y",
                           organization_id=CODE_B)]])
        self.assertEqual(blocked["state"], "failed")
        self.assertEqual(self.work.ledger.page("alpha", "same-page", 1)["error_code"],
                         "organization_identifier_conflict")
        # 整页回滚，两条记录都不得落库
        self.assertEqual(len(self.work.ledger.observations(source="alpha")), 1)

    def test_accumulating_second_identifier_does_not_block_unchanged_republish(self):
        # 同组一方正常累积第二个标识后，另一方重申原有共享标识不应被误判冲突
        self.work.ingest(key="alpha", pages=[[
            record(record_id="a1", organization="FT", organization_id=CODE_A)]])
        self.work.ingest(key="beta", pages=[[
            record(record_id="b1", organization="Fictional Tech", organization_id=CODE_A)]])
        self.work.harvester.confirm_organization(
            {"left_org": "FT", "right_org": "Fictional Tech", "basis": "manual", "reason": "x"})
        # Fictional Tech 额外获得一个登记号，与 FT 仍共享 CODE_A，可正常采入
        _, extended = self.work.ingest(
            key="beta", batch="b2",
            pages=[[record(record_id="b2", organization="Fictional Tech",
                           organization_id=CODE_B)]])
        self.assertEqual(extended["state"], "complete")
        # FT 重申未变化的 CODE_A，不应被单边集合差异误伤
        _, republished = self.work.ingest(
            key="alpha", batch="b2",
            pages=[[record(record_id="a2", organization="FT", organization_id=CODE_A)]])
        self.assertEqual(republished["state"], "complete")

    def test_transitive_confirmation_is_idempotent(self):
        self.work.register(key="gamma", trust=60)
        for key, org in (("alpha", "Alpha Org"), ("beta", "Beta Org"), ("gamma", "Gamma Org")):
            self.work.ingest(key=key, pages=[[record(record_id=key, organization=org)]])
        self.work.harvester.confirm_organization(
            {"left_org": "Alpha Org", "right_org": "Beta Org", "basis": "manual", "reason": "1"})
        self.work.harvester.confirm_organization(
            {"left_org": "Beta Org", "right_org": "Gamma Org", "basis": "manual", "reason": "2"})
        # Alpha 与 Gamma 已传递连通，再次确认应为无操作，不新增冗余决议行
        before = len(self.work.ledger.resolutions())
        result = self.work.harvester.confirm_organization(
            {"left_org": "Gamma Org", "right_org": "Alpha Org", "basis": "manual", "reason": "3"})
        self.assertTrue(result["unchanged"])
        self.assertEqual(len(self.work.ledger.resolutions()), before)


if __name__ == "__main__":
    unittest.main()
