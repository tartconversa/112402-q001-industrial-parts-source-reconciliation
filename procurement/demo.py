"""可重复运行的完整演练：样本 + 异构来源 + 公告 + 替代审批 + 复核 + 报告。

运行：``python -m procurement demo --fresh``
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import load_json
from .engine import ProcurementApp
from .reporting import build_report, write_reports
from .sources import FileTransport

ROOT = Path(__file__).resolve().parent.parent
DEMO_AS_OF = "2026-10-20"


def load_fixtures_into(app: ProcurementApp, fdir: str | Path, *,
                       collect_checkpoints: bool = True) -> dict[str, Any]:
    """按业务顺序装入 fixtures：设备台账 -> 异构目录 -> 报价 -> 公告 -> 审批 -> 新公告。"""
    fdir = Path(fdir).resolve()
    summary: dict[str, Any] = {}

    sources_cfg = fdir / "sources.json"
    if sources_cfg.exists():
        app.register_sources(sources_cfg)
        summary["sources_registered"] = len(app.store.sources)

    equipment = fdir / "equipment.csv"
    if equipment.exists():
        summary["equipment"] = app.import_assets(equipment)

    dealers = fdir / "dealers_extra.csv"
    if dealers.exists():
        summary["dealers_extra"] = app.ingest_catalog(
            dealers, source_id="S-EXTRA", adapter="dealer_wide", refresh=False)

    quotes = fdir / "quotes_w43.csv"
    if quotes.exists():
        summary["quotes"] = app.import_quotes(quotes, refresh=False)

    ann_initial = fdir / "announcements_initial.json"
    if ann_initial.exists():
        summary["announcements_initial"] = app.ingest_announcements(
            ann_initial, refresh=False)

    approvals = fdir / "approvals.json"
    if approvals.exists():
        rows = load_json(approvals)
        rows = rows.get("approvals", rows) if isinstance(rows, dict) else rows
        made = []
        for cfg in rows:
            made.append(app.approve_substitution(**cfg)["approval"]["id"])
        summary["approvals"] = made

    # “新公告”最后到达：相对已建立的 confirmed 基线属于新证据 -> 产生带证据复核事项
    lead_html = fdir / "ann_lead_alert.html"
    if lead_html.exists():
        summary["lead_alert"] = app.ingest_announcements(lead_html)

    app.refresh("fixtures_loaded")

    # 用文件传输把所有登记来源幂等地跑一遍，形成频率/检查点记录（重复运行不产生新版本）
    if collect_checkpoints and sources_cfg.exists():
        root = fdir.parent
        app.collect(force=True, transport=FileTransport(base_dir=root))
        summary["checkpoints"] = {sid: cp.get("status")
                                 for sid, cp in app.store.checkpoints.items()}
    return summary


def run_demo(*, outdir: str | Path = "output", fresh: bool = False,
             as_of: str = DEMO_AS_OF) -> dict[str, Any]:
    out = Path(outdir)
    if not out.is_absolute():
        out = ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "demo_state.json"
    if fresh and state_path.exists():
        state_path.unlink()

    app = ProcurementApp(state_path, as_of=as_of)
    sample_summary = app.ingest_samples(ROOT / "samples")
    fixtures_summary = load_fixtures_into(app, ROOT / "fixtures")
    paths = write_reports(app, out)
    report = build_report(app)

    # 额外导出一份精简的采购员待办，方便直接查看
    todo_path = out / "buyer_todo.json"
    todo_path.write_text(json.dumps(
        [{"id": r["id"], "order_id": r.get("order_id"), "material_id": r.get("material_id"),
          "code": r["code"], "severity": r["severity"], "message": r["message"],
          "suggested_action": r["suggested_action"]}
         for r in app.open_review_items()], ensure_ascii=False, indent=2), encoding="utf-8")

    return {"app": app, "report": report, "paths": {**paths, "buyer_todo": todo_path},
            "summary": {"samples": "loaded", "fixtures": fixtures_summary,
                        "catalog_new_versions": sample_summary["catalog"]["new_versions"]}}


if __name__ == "__main__":
    result = run_demo(fresh=True)
    print(json.dumps({"dashboard": result["report"]["dashboard"],
                      "paths": {k: str(v) for k, v in result["paths"].items()}},
                     ensure_ascii=False, indent=2))
