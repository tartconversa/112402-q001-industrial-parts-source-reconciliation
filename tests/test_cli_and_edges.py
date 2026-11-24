"""CLI、可重复流水线、EML/HTML 解析与若干边界行为测试。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path

from procurement import ProcurementApp, cli_main
from procurement.parsers import parse_eml, parse_html

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
FIXTURES = ROOT / "fixtures"
AS_OF = "2026-10-20"


class CliTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.state = str(Path(self._td.name) / "state.json")

    def tearDown(self):
        self._td.cleanup()

    def _run(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(list(argv))
        return code, buf.getvalue()

    def test_cli_check_and_material_json(self):
        app = ProcurementApp(self.state, as_of=AS_OF)
        app.ingest_samples(SAMPLES)
        code, out = self._run("--state", self.state, "--as-of", AS_OF,
                              "check", "PO-31", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["can_release"])

        code, out = self._run("material", "BR-620", "--state", self.state, "--json")
        self.assertEqual(code, 0)
        self.assertIn("source_differences", json.loads(out))

    def test_cli_blocked_returns_nonzero(self):
        app = ProcurementApp(self.state, as_of=AS_OF)
        app.ingest_samples(SAMPLES)
        code, _ = self._run("--state", self.state, "--as-of", AS_OF, "check", "PO-32")
        self.assertEqual(code, 2)

    def test_cli_reviews_json(self):
        app = ProcurementApp(self.state, as_of=AS_OF)
        app.ingest_samples(SAMPLES)
        code, out = self._run("reviews", "--state", self.state, "--json")
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(out), list)


class PipelineRepeatTests(unittest.TestCase):
    def test_run_pipeline_twice_idempotent(self):
        from pipeline import run_pipeline
        with tempfile.TemporaryDirectory() as td:
            state = str(Path(td) / "state.json")
            out = str(Path(td) / "out")
            r1 = run_pipeline(samples=str(SAMPLES), fixtures=str(FIXTURES),
                              state_path=state, outdir=out)
            versions_1 = len(r1["report"]["materials"])
            app2 = ProcurementApp(state, as_of=AS_OF)
            n_versions = len(app2.store.record_versions)
            # 再跑一次（fresh=False）不应让原始版本翻倍
            r2 = run_pipeline(samples=str(SAMPLES), fixtures=str(FIXTURES),
                              state_path=state, outdir=out, fresh=False)
            app3 = ProcurementApp(state, as_of=AS_OF)
            self.assertEqual(len(app3.store.record_versions), n_versions)
            self.assertEqual(len(r2["report"]["materials"]), versions_1)
            self.assertTrue(Path(r2["paths"]["markdown"]).exists())

    def test_demo_run_directly(self):
        from procurement.demo import run_demo
        with tempfile.TemporaryDirectory() as td:
            r = run_demo(outdir=td, fresh=True)
            self.assertIn("dashboard", r["report"])
            self.assertTrue(Path(r["paths"]["buyer_todo"]).exists())


class ParserEdgeTests(unittest.TestCase):
    def test_parse_html_announcement_table(self):
        rows = parse_html(FIXTURES / "ann_lead_alert.html")
        self.assertTrue(rows)
        joined = json.dumps(rows, ensure_ascii=False)
        self.assertIn("A-LEAD-BR620", joined)
        self.assertIn("45", joined)

    def test_eml_csv_attachment_ingests_announcement(self):
        msg = EmailMessage()
        msg["Subject"] = "CT-50 EOL"
        msg.set_content("CT-50 停产，替代 CT-55")
        csv_text = ("announcement_id,model,kind,effective_date,replacement_model\n"
                    "A1,CT-50,discontinuation,2026-12-01,CT-55\n")
        msg.add_attachment(csv_text.encode(), maintype="text", subtype="csv",
                           filename="a.csv")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.eml"
            p.write_bytes(bytes(msg))
            rows = parse_eml(p)
            app = ProcurementApp(as_of=AS_OF)
            app.ingest_announcements(rows, source_id="EMAIL")
            self.assertIn("A1", app.store.announcements)

    def test_body_only_eml_extracts_lead_days(self):
        rows = parse_eml(self._make_body_eml())
        app = ProcurementApp(as_of=AS_OF)
        app.ingest_announcements(rows, source_id="EMAIL")
        ann = next(iter(app.store.announcements.values()))
        self.assertEqual(ann["model"], "BR-620")
        self.assertEqual(ann["lead_days"], 45)
        self.assertEqual(ann["kind"], "lead_time")

    @staticmethod
    def _make_body_eml() -> Path:
        msg = EmailMessage()
        msg["Subject"] = "交期调整"
        msg.set_content("请注意 BR-620 的 lead time 延长到 45 天。")
        td = tempfile.mkdtemp()
        p = Path(td) / "b.eml"
        p.write_bytes(bytes(msg))
        return p


if __name__ == "__main__":
    unittest.main()
