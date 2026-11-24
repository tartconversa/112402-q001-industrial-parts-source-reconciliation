"""端到端行为测试：覆盖采集续采、证据链、物料核验、替代审批、订单复核与重算。

这些测试以题目业务规则为验收标准，全部使用标准库 unittest 与临时状态文件，
不访问网络。测试 docstring 使用中文描述业务意图。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from procurement import ProcurementApp
from procurement.sources import ScriptedTransport
from procurement.specs import (build_spec, evaluate_identity, model_relation,
                              value_covers)

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
AS_OF = "2026-10-20"


def make_app(tmp: str | None = None) -> ProcurementApp:
    state = str(Path(tmp) / "state.json") if tmp else None
    app = ProcurementApp(state, as_of=AS_OF)
    app.ingest_samples(SAMPLES)
    return app


class IdentityRulesTests(unittest.TestCase):
    def test_separator_normalization_but_case_sensitive(self):
        """BR-620 与 BR620 仅分隔符不同 -> normalized_code；大小写不同 -> case_only。"""
        self.assertEqual(model_relation("BR-620", "BR620"), "normalized_code")
        self.assertEqual(model_relation("BR620", "br620"), "case_only")
        self.assertEqual(model_relation("CT-40", "CT-45"), "similar")

    def test_load_conflict_blocks_confirmation(self):
        """厂商编码一致但额定负载 high vs medium 冲突时必须待核，不得确认同一物料。"""
        a = build_spec(model="BR620", load="high")
        b = build_spec(model="BR620", load="medium")
        verdict = evaluate_identity(a, b)
        self.assertEqual(verdict["status"], "pending")
        self.assertTrue(any(r["code"] == "parameter_conflict" and r.get("attribute") == "load"
                            for r in verdict["reasons"]))

    def test_missing_rating_is_insufficient(self):
        """编码一致但双方都没有额定参数 -> 证据不足，保持待核。"""
        a = build_spec(model="CT-40")
        b = build_spec(model="CT40")
        verdict = evaluate_identity(a, b)
        self.assertEqual(verdict["status"], "pending")
        self.assertTrue(any("insufficient" in r["code"] or r["code"].startswith("missing")
                            for r in verdict["reasons"]))

    def test_dimension_conflict(self):
        """安装尺寸不一致必须判为 dimension_conflict。"""
        a = build_spec(model="X1", bore_mm=50, width_mm=20)
        b = build_spec(model="X1", bore_mm=50, width_mm=25)
        verdict = evaluate_identity(a, b)
        self.assertTrue(any(r["code"] == "dimension_conflict" for r in verdict["reasons"]))
        self.assertEqual(evaluate_identity(
            build_spec(model="X1", bore_mm=50, width_mm=20),
            build_spec(model="X-1", bore_mm=50, width_mm=20))["status"], "confirmed")

    def test_similarity_alone_never_confirms(self):
        """高度相似但编码不同且无匹配参数时，不能 confirmed。"""
        a = build_spec(model="BR-6200", load="high")
        b = build_spec(model="BR-6201", load="high")
        verdict = evaluate_identity(a, b)
        self.assertNotEqual(verdict["status"], "confirmed")

    def test_parameter_range_coverage(self):
        """审批参数范围支持离散值、等级下限与数值区间。"""
        high = {"kind": "level", "name": "load", "value": "high", "level": 4}
        self.assertTrue(value_covers("load", high, {"min": "high"})["covered"])
        medium = {"kind": "level", "name": "load", "value": "medium", "level": 3}
        self.assertFalse(value_covers("load", medium, {"values": ["high"]})["covered"])
        num = {"kind": "number", "name": "control_channels", "value": 6}
        self.assertTrue(value_covers("control_channels", num, {"min": 4, "max": 8})["covered"])
        self.assertFalse(value_covers("control_channels", num, {"min": 7})["covered"])


class SampleScenarioTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.app = make_app(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_three_orders_present(self):
        self.assertEqual(set(self.app.store.orders), {"PO-31", "PO-32", "PO-33"})

    def test_po31_clean_direct_release(self):
        """PO-31：编码分隔符差异可归一、负载一致、有库存 -> 可直接放行。"""
        chk = self.app.check_order("PO-31")
        self.assertEqual(chk["verdict"], "releasable")
        self.assertTrue(chk["can_release"])
        self.assertEqual(chk["release_kind"], "direct")

    def test_po32_load_conflict_not_interchangeable(self):
        """PO-32：X-8 负载 medium 与 M2 要求 high 冲突 -> 不得放行。"""
        chk = self.app.check_order("PO-32")
        self.assertFalse(chk["can_release"])
        codes = [b["code"] for b in chk["blockers"]] + [h["code"] for h in chk["holds"]]
        self.assertIn("parameter_conflict", codes)
        self.assertIsNone(chk["release_kind"])

    def test_po33_out_of_stock_and_pending_evidence(self):
        """PO-33：I-12 库存 0 且额定参数缺失 -> 阻断 + 待核原因。"""
        chk = self.app.check_order("PO-33")
        self.assertFalse(chk["can_release"])
        self.assertIn("out_of_stock", [b["code"] for b in chk["blockers"]])
        hold_codes = [h["code"] for h in chk["holds"]]
        self.assertTrue(any("insufficient" in c or c.startswith("missing") for c in hold_codes))

    def test_material_view_shows_source_attribute_difference(self):
        """物料视图必须展示 S1 high 与 S2 medium 的来源差异。"""
        mv = self.app.material_view("BR-620")
        load_diff = next(d for d in mv["source_differences"] if d["attribute"] == "load")
        values = {v["value"] for v in load_diff["values"]}
        self.assertEqual(values, {"high", "medium"})
        self.assertTrue(load_diff["conflict"])
        self.assertTrue(mv["pending_reasons"])
        self.assertTrue(mv["next_actions"])


class CollectionCheckpointTests(unittest.TestCase):
    def test_mid_source_outage_resumes_without_duplicates(self):
        """第二批时来源不可用：offset 停在 2；恢复后从位置续采且不重复。"""
        app = ProcurementApp(as_of=AS_OF)
        app.register_source({"id": "SX", "kind": "catalog", "batch_size": 2})
        rows = [{"item": f"I-{i}", "model": "BR-620", "load": "high", "available": i}
                for i in range(5)]
        failed = app.run_source("SX", transport=ScriptedTransport({"SX": rows}, fail_at={"SX": 2}))
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(app.store.checkpoints["SX"]["offset"], 2)

        ok = app.run_source("SX", transport=ScriptedTransport({"SX": rows}))
        self.assertEqual(ok["status"], "success")
        self.assertEqual(ok["records_seen"], 3)  # 仅续采剩余 3 条
        sx_items = [k for k in app.store.listings if k.startswith("SX::")]
        self.assertEqual(len(sx_items), 5)

    def test_repeat_run_is_idempotent_versions(self):
        """重复成功采集不产生新的原始版本。"""
        app = ProcurementApp(as_of=AS_OF)
        app.register_source({"id": "SZ", "kind": "catalog"})
        rows = [{"item": "I-1", "model": "CT-40"}]
        app.run_source("SZ", transport=ScriptedTransport({"SZ": rows}))
        again = app.run_source("SZ", transport=ScriptedTransport({"SZ": rows}))
        self.assertEqual(again["new_versions"], 0)
        self.assertEqual(len(app.store.listings), 1)

    def test_frequency_gating(self):
        """日频来源在未到频率时 skipped，到期后运行。"""
        import datetime as dt
        app = ProcurementApp(as_of=AS_OF)
        app.register_source({"id": "SF", "kind": "catalog", "frequency": "daily"})
        t = ScriptedTransport({"SF": [{"item": "I-1"}]})
        now = dt.datetime(2026, 10, 20, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        app.collect(transport=t, now=now, force=True)
        immediate = app.collect(transport=t, now=now)
        self.assertEqual(immediate[0]["status"], "skipped_not_due")
        later = app.collect(transport=t, now=now + dt.timedelta(hours=25))
        self.assertEqual(later[0]["status"], "success")

    def test_file_transport_reads_sample_catalog(self):
        """FileTransport 能直接读样本 CSV 并保留行级来源 S1/S2。"""
        app = ProcurementApp(as_of=AS_OF)
        app.register_source({"id": "CAT", "kind": "catalog", "adapter": "sample_catalog",
                             "path": str(SAMPLES / "catalog.csv")})
        reps = app.collect(force=True)
        self.assertEqual(reps[0]["status"], "success")
        self.assertIn("S1::I-11", app.store.listings)
        self.assertIn("S2::X-8", app.store.listings)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_listing_has_full_provenance(self):
        """每条供货事实可追溯来源、抓取时间、原始版本与哈希。"""
        prov = app_listing_prov(self.app, "S1::I-11")
        for key in ("source_id", "version_id", "payload_hash", "fetched_at", "adapter_version"):
            self.assertTrue(prov.get(key), key)
        version = next(v for v in self.app.store.record_versions
                       if v["version_id"] == prov["version_id"])
        self.assertEqual(version["payload"]["item"], "I-11")
        self.assertEqual(len(version["payload_hash"]), 64)

    def test_state_persists_and_reloads(self):
        """状态落盘后用新 App 重载，事实与结论不丢。"""
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "state.json")
            app = ProcurementApp(path, as_of=AS_OF)
            app.ingest_samples(SAMPLES)
            app2 = ProcurementApp(path, as_of=AS_OF)
            self.assertIn("PO-32", app2.store.orders)
            self.assertGreaterEqual(len(app2.store.record_versions), 3)


def app_listing_prov(app, key):
    return (app.store.listings[key].get("catalog") or {}).get("provenance") or {}


class AnnouncementReviewTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.app = make_app(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_new_lead_announcement_creates_evidence_review_for_confirmed_po(self):
        """新公告把交期延长到超过 PO-31 需求日 -> 生成带证据的交付复核事项。"""
        result = self.app.add_announcement({
            "announcement_id": "A-LEAD", "model": "BR-620", "kind": "lead_time",
            "published_at": "2026-10-18T09:00+08:00", "effective_date": "2026-10-18",
            "lead_days": 45})
        delivery_ids = result["impact"]["review_sync"]["new_delivery_reviews"]
        self.assertTrue(delivery_ids)
        rv = self.app.store.review_items[delivery_ids[0]]
        self.assertEqual(rv["code"], "delivery_date_superseded")
        self.assertEqual(rv["order_id"], "PO-31")
        kinds = {e.get("kind") for e in rv["evidence"]}
        self.assertIn("announcement", kinds)
        self.assertIn("source_record", kinds)
        self.assertTrue(rv["suggested_action"])

    def test_effective_discontinuation_blocks(self):
        """已生效停产公告必须阻断候选。"""
        self.app.add_announcement({"announcement_id": "EOL", "model": "BR-620",
                                   "kind": "discontinuation", "effective_date": "2026-10-01"})
        chk = self.app.check_order("PO-31")
        self.assertIn("discontinued_effective", [b["code"] for b in chk["blockers"]])

    def test_old_quote_cannot_erase_discontinuation(self):
        """时间较旧但重新导入的大库存短交期报价，不能覆盖生效停产结论。"""
        self.app.add_announcement({"announcement_id": "EOL2", "model": "BR-620",
                                   "kind": "discontinuation", "effective_date": "2026-10-01"})
        self.app.import_quotes([{"quote_id": "Q-OLD", "source": "S1", "item": "I-11",
                                 "model": "BR-620", "available": 999, "lead_days": 1,
                                 "quote_date": "2026-09-01"}])
        chk = self.app.check_order("PO-31")
        self.assertIn("discontinued_effective", [b["code"] for b in chk["blockers"]])

    def test_withdraw_announcement_recomputes_and_resolves_review(self):
        """撤回公告后：未下单需求重算，对应复核事项自动解决并留痕。"""
        self.app.add_announcement({"announcement_id": "EOL-CT40", "model": "CT-40",
                                   "kind": "discontinuation", "effective_date": "2026-10-01"})
        self.assertTrue(any(r.get("code") == "discontinued_effective"
                            for r in self.app.open_review_items()))
        self.app.withdraw_announcement("EOL-CT40", reason="厂商撤回")
        last = self.app.store.recomputations[-1]
        self.assertEqual(last["trigger"], "announcement_withdrawn")
        self.assertIn("PO-33", [a["order_id"] for a in last["affected"]])
        eol_items = [r for r in self.app.store.review_items.values()
                     if r.get("code") == "discontinued_effective"]
        self.assertTrue(eol_items)
        self.assertTrue(all(r["status"] == "resolved" for r in eol_items))
        self.assertTrue(all(r.get("resolution_note") for r in eol_items))


class SubstitutionApprovalTests(unittest.TestCase):
    def _app_with_substitute(self):
        app = ProcurementApp(as_of=AS_OF)
        app.ingest_catalog([
            {"source": "S1", "item": "I-12", "model": "CT-40", "available": 0},
            {"source": "S4", "item": "N-45", "model": "CT-45",
             "control_channels": 6, "available": 12, "lead_days": 10}], refresh=False)
        # 显式提供设备型号范围（样本 assets.csv 无该列，可由设备台账补充）
        app.store.assets["M3"] = {
            "id": "M3", "asset": "M3", "model": "CT-40",
            "equipment_model": "PACKER-300",
            "spec": build_spec(model="CT-40"), "raw": {}}
        app.import_orders([{"order_id": "PO-33", "asset": "M3", "item": "I-12",
                            "due": "2026-11-09", "state": "draft"}])
        return app

    def test_both_equipment_and_param_scope_approves_release(self):
        """设备型号与参数范围均覆盖时，替代件才可释放。"""
        app = self._app_with_substitute()
        app.approve_substitution(from_model="CT-40", to_model="CT-45",
                                 equipment_models=["PACKER-300"],
                                 parameter_ranges={"control_channels": {"min": 4, "max": 8}},
                                 approval_id="AP-OK")
        chk = app.check_order("PO-33")
        self.assertTrue(chk["can_release"])
        self.assertEqual(chk["release_kind"], "approved_substitution")
        self.assertEqual(chk["recommended"]["approval_id"], "AP-OK")

    def test_equipment_not_in_scope_blocks(self):
        """设备型号不在批准范围 -> 明确的 equipment 原因且不放行。"""
        app = self._app_with_substitute()
        app.approve_substitution(from_model="CT-40", to_model="CT-45",
                                 equipment_models=["OTHER-9"],
                                 parameter_ranges={"control_channels": [4, 8]})
        chk = app.check_order("PO-33")
        self.assertFalse(chk["can_release"])
        sub = chk["substitution_options"][0]
        self.assertFalse(sub["approval_scope"]["equipment"]["approved"])
        self.assertTrue(any(b["code"] == "substitution_equipment_not_approved"
                            for b in sub["blockers"]))

    def test_parameter_out_of_range_blocks(self):
        """替代件实际参数超出批准范围 -> parameter_out_of_range。"""
        app = self._app_with_substitute()
        app.approve_substitution(from_model="CT-40", to_model="CT-45",
                                 equipment_models=["PACKER-300"],
                                 parameter_ranges={"control_channels": {"min": 7}})
        chk = app.check_order("PO-33")
        self.assertFalse(chk["can_release"])
        sub = chk["substitution_options"][0]
        self.assertFalse(sub["approval_scope"]["parameters"]["approved"])
        self.assertTrue(any(b["code"] == "substitution_parameter_out_of_range"
                            for b in sub["blockers"]))

    def test_empty_scope_is_not_blanket_approval(self):
        """缺少设备范围或参数范围的审批不能作为万能许可。"""
        app = self._app_with_substitute()
        app.approve_substitution(from_model="CT-40", to_model="CT-45",
                                 equipment_models=["PACKER-300"], parameter_ranges={})
        chk = app.check_order("PO-33")
        self.assertFalse(chk["can_release"])


class ConfirmedOrderImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.app = make_app(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_catalog_refresh_does_not_rewrite_confirmed_order(self):
        """普通目录刷新（含库存变化）不得改写已确认订单头或原始快照校验和。"""
        snap_checksum = self.app.store.orders["PO-31"]["snapshot"]["checksum"]
        due = self.app.store.orders["PO-31"]["due"]
        # 重新采集目录，I-11 变为 0 库存
        self.app.ingest_catalog([
            {"source": "S1", "item": "I-11", "model": "BR-620", "load": "high",
             "available": 0, "updated": "2026-10-21T09:00+08:00"},
            {"source": "S2", "item": "X-8", "model": "BR620", "load": "medium",
             "available": 6, "updated": "2026-10-14T10:30+08:00"},
            {"source": "S1", "item": "I-12", "model": "CT-40", "load": "",
             "available": 0, "updated": "2026-10-13T16:00+08:00"}],
            refresh=True)
        order = self.app.store.orders["PO-31"]
        self.assertEqual(order["due"], due)
        self.assertEqual(order["state"], "confirmed")
        self.assertEqual(order["snapshot"]["checksum"], snap_checksum)
        # 但当前风险中应能看到新的缺货
        codes = [r["code"] for r in self.app.order_view("PO-31")["current_risks"]]
        self.assertIn("out_of_stock", codes)

    def test_mapping_correction_preserves_decision_but_adds_risk(self):
        """更正已下单物料映射：原决策保留，订单视图出现 mapping_corrected 风险。"""
        original = self.app.store.orders["PO-31"]["original_decision"]["verdict"]
        self.app.correct_mapping(item="I-11", source="S1", to_material="MAT-ELSE")
        order = self.app.store.orders["PO-31"]
        self.assertEqual(order["original_decision"]["verdict"], original)
        risk_codes = [r["code"] for r in self.app.order_view("PO-31")["current_risks"]]
        self.assertIn("mapping_corrected", risk_codes)

    def test_confirm_clean_draft_then_refresh_keeps_snapshot(self):
        """干净草稿可确认；确认后快照固化，后续刷新不改写。"""
        app = ProcurementApp(as_of=AS_OF)
        app.ingest_catalog([{"source": "S1", "item": "K1", "model": "BR-700",
                             "load": "high", "available": 8, "lead_days": 5}], refresh=False)
        app.import_assets([{"asset": "A1", "model": "BR-700", "load": "high"}])
        app.import_orders([{"order_id": "O1", "asset": "A1", "item": "K1",
                            "due": "2026-11-10", "state": "draft"}])
        self.assertTrue(app.check_order("O1")["can_release"])
        res = app.confirm_order("O1", by="buyer-li")
        self.assertTrue(res["confirmed"])
        checksum = app.store.orders["O1"]["snapshot"]["checksum"]
        app.ingest_catalog([{"source": "S1", "item": "K1", "model": "BR-700",
                             "load": "high", "available": 0}], refresh=True)
        self.assertEqual(app.store.orders["O1"]["snapshot"]["checksum"], checksum)
        self.assertEqual(app.store.orders["O1"]["state"], "confirmed")

    def test_confirm_blocked_draft_refused_without_force(self):
        """采购前核对不通过时，默认不能确认订单。"""
        res = self.app.confirm_order("PO-32")
        self.assertFalse(res["confirmed"])
        self.assertEqual(self.app.store.orders["PO-32"]["state"], "draft")


class SubstitutionRangeSemanticsTests(unittest.TestCase):
    def _app(self, candidate: dict, asset_spec) -> ProcurementApp:
        app = ProcurementApp(as_of=AS_OF)
        app.ingest_catalog([candidate], refresh=False)
        app.store.assets["E"] = {"id": "E", "asset": "E", "model": "CT-40",
                                 "equipment_model": "DEV", "spec": asset_spec, "raw": {}}
        app.import_orders([{"order_id": "O", "asset": "E", "item": "ZZ",
                            "due": "2026-11-10"}])
        return app

    def test_parameter_may_differ_but_stay_inside_range(self):
        """替代件参数不必与设备原值完全相等；落在批准范围内即放行。"""
        app = self._app(
            {"source": "S9", "item": "B", "model": "CT-55", "load": "extra_high",
             "available": 5, "lead_days": 3},
            build_spec(model="CT-40", load="high"))
        app.approve_substitution(from_model="CT-40", to_model="CT-55",
                                 equipment_models=["DEV"],
                                 parameter_ranges={"load": {"min": "high"}})
        self.assertTrue(app.check_order("O")["can_release"])

    def test_outside_range_or_unapproved_dimension_blocks(self):
        """超出参数范围，或批准范围未覆盖的尺寸冲突，都不得借替代放行。"""
        # 1) 负载低于批准下限
        app = self._app(
            {"source": "S9", "item": "B", "model": "CT-66", "load": "medium",
             "available": 5},
            build_spec(model="CT-40", load="high"))
        app.approve_substitution(from_model="CT-40", to_model="CT-66",
                                 equipment_models=["DEV"],
                                 parameter_ranges={"load": {"min": "high"}})
        self.assertFalse(app.check_order("O")["can_release"])

        # 2) 负载一致但宽度冲突，且审批范围只管负载 -> 尺寸冲突仍阻断
        app2 = self._app(
            {"source": "S9", "item": "C", "model": "CT-77", "load": "high",
             "width_mm": 25, "available": 5},
            build_spec(model="CT-40", load="high", width_mm=20))
        app2.approve_substitution(from_model="CT-40", to_model="CT-77",
                                  equipment_models=["DEV"],
                                  parameter_ranges={"load": {"values": ["high"]}})
        chk = app2.check_order("O")
        self.assertFalse(chk["can_release"])
        sub = chk["substitution_options"][0]
        self.assertTrue(any(b["code"] == "dimension_conflict" for b in sub["blockers"]))


class ViewAndDashboardTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def test_order_view_structure(self):
        ov = self.app.order_view("PO-32")
        self.assertIn("check", ov)
        self.assertIn("material_overview", ov)
        self.assertTrue(ov["material_overview"]["source_differences"])

    def test_dashboard_counts(self):
        d = self.app.dashboard()
        self.assertEqual(d["counts"]["draft_orders"], 2)
        self.assertEqual(d["counts"]["confirmed_orders"], 1)
        self.assertGreaterEqual(d["counts"]["open_review_items"], 1)
        self.assertIn("source_checkpoints", d)


if __name__ == "__main__":
    unittest.main()
