"""生成可交给采购员/治理人员阅读的 JSON 与 Markdown 报告。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import dump_json_atomic
from .status import STATUS_LABELS


def build_report(app) -> dict[str, Any]:
    dash = app.dashboard()
    materials = [app.material_view(m["id"]) for m in app.store.materials.values()]
    orders = [app.order_view(oid) for oid in app.store.orders]
    return {
        "generated_for": "关键零部件供应变化采集核验与替代确认",
        "as_of": app.as_of_date().isoformat(),
        "dashboard": dash["counts"],
        "review_items": dash["review_items"],
        "materials": materials,
        "orders": orders,
        "checkpoints": list(app.store.checkpoints.values()),
        "sources": list(app.store.sources.values()),
        "runs": app.store.data["runs"][-20:],
        "recomputations": app.store.data["recomputations"][-10:],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 采购采集核验报告")
    lines.append("")
    lines.append(f"- 核对基准日（as_of）：**{report['as_of']}**")
    c = report["dashboard"]
    lines.append(
        f"- 物料 {c['materials']}（待核 {c['pending_materials']}）；"
        f"草稿需求 {c['draft_orders']}（可放行 {c['draft_releasable']} / "
        f"阻断 {c['draft_blocked']} / 待核 {c['draft_pending']}）；"
        f"已确认 {c['confirmed_orders']}")
    lines.append(f"- 待处理复核事项 **{c['open_review_items']}**（阻断级 {c['blocker_reviews']}）")
    lines.append("")

    lines.append("## 一、采购员待办（复核事项）")
    lines.append("")
    if not report["review_items"]:
        lines.append("_无待处理事项。_")
    for rv in report["review_items"]:
        lines.append(f"### [{rv.get('severity')}] {rv['id']} · {rv.get('title')}")
        lines.append(f"- 对象：{rv.get('subject_type')} `{rv.get('subject_id')}`；类型码：`{rv.get('code')}`")
        lines.append(f"- 说明：{rv.get('message')}")
        lines.append(f"- 后续处置：{rv.get('suggested_action')}")
        evs = rv.get("evidence") or []
        if evs:
            lines.append("- 证据：")
            for e in evs[:6]:
                lines.append(
                    f"  - {e.get('kind')} source={e.get('source_id')} "
                    f"version={e.get('version_id')} fetched_at={e.get('fetched_at')} "
                    f"hash={(e.get('payload_hash') or '')[:10]}")
        lines.append("")

    lines.append("## 二、按订单核对")
    lines.append("")
    for ov in report["orders"]:
        o = ov["order"]
        lines.append(f"### {o['order_id']} · {o.get('state')} · 需求日 {o.get('due')}")
        lines.append(f"- 设备 `{o.get('asset')}`，指定编码 `{o.get('item')}`")
        if ov.get("placed"):
            od = ov.get("original_decision") or {}
            lines.append(f"- 原始决策（保留不改写）：{od.get('verdict')} / {od.get('release_kind')} / {od.get('at')}")
            risks = ov.get("current_risks", [])
            if risks:
                lines.append("- 当前新风险：")
                for rk in risks:
                    tag = "（新公告）" if rk.get("new_announcement") else ""
                    lines.append(f"  - [{rk.get('severity')}] `{rk.get('code')}`{tag}：{rk.get('message')}")
            else:
                lines.append("- 当前无新增风险。")
        chk = ov.get("check") or {}
        if chk:
            lines.append(f"- 当前核对结论：**{chk.get('verdict')}**，可放行={chk.get('can_release')}，"
                         f"放行方式={chk.get('release_kind')}")
            for b in chk.get("blockers", []):
                lines.append(f"  - 阻断 `{b.get('code')}`：{b.get('message')}")
            for h in chk.get("holds", []):
                lines.append(f"  - 待核 `{h.get('code')}`：{h.get('message')}")
            if chk.get("recommended"):
                rec = chk["recommended"]
                if rec.get("kind") == "approved_substitution":
                    rec_text = (f"批准替代 {rec.get('approval_id')} -> "
                                f"{rec.get('source_id')}/{rec.get('item')}")
                else:
                    rec_text = f"{rec.get('source_id')}/{rec.get('item')}"
                lines.append(f"  - 建议来源（{chk.get('release_kind')}）：{rec_text}")
        lines.append("")

    lines.append("## 三、按物料：来源差异与待核原因")
    lines.append("")
    for mv in report["materials"]:
        m = mv["material"]
        lines.append(f"### {m['id']} · {m.get('canonical_model')} · 治理状态：{m.get('governance_status_label')}")
        if mv["source_differences"]:
            lines.append("- 来源差异：")
            for d in mv["source_differences"]:
                vals = ", ".join(f"{v['source_id']}/{v['item']}={v['value']}" for v in d["values"])
                flag = " ⚠冲突" if d.get("conflict") else ""
                lines.append(f"  - `{d['attribute']}`{flag}：{vals}")
        if mv["pending_reasons"]:
            lines.append("- 待核原因：")
            for r in mv["pending_reasons"]:
                lines.append(f"  - `{r.get('code')}`：{r.get('message')}（{r.get('source_id')}/{r.get('item')}）")
        if mv["next_actions"]:
            lines.append("- 后续处置：")
            for a in dict.fromkeys(mv["next_actions"]):
                lines.append(f"  - {a}")
        lines.append("")

    lines.append("## 四、采集来源与检查点")
    lines.append("")
    for cp in report["checkpoints"]:
        lines.append(f"- `{cp.get('source_id')}` status={cp.get('status')} "
                     f"offset={cp.get('offset')} done={cp.get('done')} "
                     f"位置={cp.get('position_description')} last_error={cp.get('last_error')}")
    lines.append("")
    return "\n".join(lines)


def write_reports(app, outdir: str | Path = "output") -> dict[str, Path]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    report = build_report(app)
    json_path = out / "procurement_report.json"
    md_path = out / "procurement_report.md"
    dump_json_atomic(json_path, report)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    if app.store.state_path is not None:
        state_target = Path(app.store.state_path)
    else:
        state_target = out / "state.json"
        dump_json_atomic(state_target, app.store.data)
    return {"json": json_path, "markdown": md_path, "state": state_target}
