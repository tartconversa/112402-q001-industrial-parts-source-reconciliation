"""命令行入口。示例：

.. code-block:: bash

    python -m procurement demo --fresh
    python -m procurement run --samples samples --fixtures fixtures --out output
    python -m procurement check PO-31 --json
    python -m procurement material BR-620
    python -m procurement reviews
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import ProcurementApp
from .reporting import build_report, render_markdown, write_reports

DEFAULT_STATE = "output/state.json"


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _app(args: argparse.Namespace) -> ProcurementApp:
    return ProcurementApp(args.state, as_of=getattr(args, "as_of", None))


def _safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------

def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo
    result = run_demo(outdir=args.out, fresh=args.fresh)
    if args.json:
        _print(result["report"], True)
    else:
        print(render_markdown(result["report"]))
        print(f"\n报告已写入：{result['paths']['markdown']} / {result['paths']['json']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.fresh and Path(args.state).exists():
        Path(args.state).unlink()
    app = _app(args)
    summary: dict[str, Any] = {}
    samples = Path(args.samples)
    if samples.exists():
        summary["samples"] = _safe(app.ingest_samples(samples))
    if args.fixtures and Path(args.fixtures).exists():
        from .demo import load_fixtures_into
        summary["fixtures"] = _safe(load_fixtures_into(app, Path(args.fixtures)))
    app.refresh("cli_run")
    paths = write_reports(app, args.out)
    report = build_report(app)
    if args.json:
        _print({"summary": summary,
                "paths": {k: str(v) for k, v in paths.items()},
                "dashboard": report["dashboard"]}, True)
    else:
        print(render_markdown(report))
        print(f"\n报告：{paths['markdown']}\nJSON：{paths['json']}\n状态库：{paths['state']}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    app = _app(args)
    if args.kind == "catalog":
        res = app.ingest_catalog(args.path, source_id=args.source or "catalog-feed",
                                 adapter=args.adapter or "generic")
    elif args.kind == "quotes":
        res = app.import_quotes(args.path, source_id=args.source or "quote-import",
                                adapter=args.adapter or "quote_standard")
    elif args.kind == "announcements":
        res = app.ingest_announcements(args.path, source_id=args.source or "mfr")
    elif args.kind == "assets":
        res = {"count": app.import_assets(args.path)}
    else:
        res = app.import_orders(args.path)
    _print(res, args.json)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    app = _app(args)
    if args.config:
        app.register_sources(args.config)
    _print(app.collect(force=args.force), args.json)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    app = _app(args)
    chk = app.check_order(args.order_id, as_of=args.as_of)
    if args.json:
        _print(chk, True)
        return 0 if chk["can_release"] else 2
    print(f"订单 {chk['order_id']} 结论：{chk['verdict']}；可放行={chk['can_release']}；"
          f"方式={chk['release_kind']}")
    for b in chk["blockers"]:
        print(f"  [阻断 {b['code']}] {b['message']}")
    for h in chk["holds"]:
        print(f"  [待核 {h['code']}] {h['message']}")
    if chk.get("recommended"):
        print(f"  建议：{chk['recommended']}")
    for a in chk.get("same_material_alternatives", []):
        print(f"  同源可选：{a['source_id']}/{a['item']} releasable={a['releasable']}")
    return 0 if chk["can_release"] else 2


def _order_text(view: dict[str, Any]) -> str:
    o = view["order"]
    lines = [f"订单 {o['order_id']} state={o['state']} asset={o['asset']} "
             f"item={o['item']} due={o['due']}"]
    if view.get("placed"):
        od = view.get("original_decision") or {}
        lines.append(f"  原始决策（不可变）: {od.get('verdict')} / "
                     f"{od.get('release_kind')} @ {od.get('at')}")
        for r in view.get("current_risks", []):
            tag = "（新公告）" if r.get("new_announcement") else ""
            lines.append(f"  当前风险 [{r['severity']}] {r['code']}{tag}: {r['message']}")
    chk = view.get("check") or {}
    if chk:
        lines.append(f"  当前核对: {chk.get('verdict')} can_release={chk.get('can_release')}")
        for b in chk.get("blockers", []):
            lines.append(f"    阻断 {b['code']}: {b['message']}")
        for h in chk.get("holds", []):
            lines.append(f"    待核 {h['code']}: {h['message']}")
    for rv in view.get("review_items", []):
        lines.append(f"  复核 {rv['id']} [{rv['code']}] {rv['message']} "
                     f"-> {rv['suggested_action']}")
    return "\n".join(lines)


def cmd_order(args: argparse.Namespace) -> int:
    view = _app(args).order_view(args.order_id, as_of=args.as_of)
    _print(view if args.json else _order_text(view), args.json)
    return 0


def cmd_material(args: argparse.Namespace) -> int:
    view = _app(args).material_view(args.identifier)
    if args.json:
        _print(view, True)
        return 0
    m = view["material"]
    lines = [f"物料 {m['id']} model={m['canonical_model']} "
             f"治理状态={m['governance_status_label']}", "  来源:"]
    for s in view["sources"]:
        lines.append(f"   - {s['source_id']}/{s['item']} model={s['model']} "
                     f"attrs={s['attributes']} avail={s['available']} price={s['price']} "
                     f"lead={s['lead_days']} mapping={s['mapping_status']} "
                     f"fetched={s['fetched_at']} ver={s['version_id']}")
    for d in view["source_differences"]:
        vals = ", ".join(f"{v['source_id']}={v['value']}" for v in d["values"])
        lines.append(f"  差异 {d['attribute']}"
                     f"{' [冲突]' if d['conflict'] else ''}: {vals}")
    for r in view["pending_reasons"]:
        lines.append(f"  待核 {r.get('code')}: {r.get('message')}")
    for a in dict.fromkeys(view["next_actions"]):
        lines.append(f"  处置: {a}")
    print("\n".join(lines))
    return 0


def cmd_reviews(args: argparse.Namespace) -> int:
    items = _app(args).open_review_items()
    if args.json:
        _print(items, True)
        return 0
    if not items:
        print("没有待处理复核事项。")
    for r in items:
        print(f"[{r['severity']}] {r['id']} {r['subject_type']}:{r['subject_id']} "
              f"{r['code']} - {r['message']}\n    -> {r['suggested_action']}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    d = _app(args).dashboard()
    _print(d if args.json else json.dumps(d["counts"], ensure_ascii=False, indent=2), args.json)
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    res = _app(args).confirm_order(args.order_id, by=args.by, force=args.force)
    out = {"confirmed": res["confirmed"], "order_id": args.order_id,
           "verdict": res.get("check", {}).get("verdict"),
           "codes": [b["code"] for b in res.get("check", {}).get("blockers", [])]
           + [h["code"] for h in res.get("check", {}).get("holds", [])]}
    _print(out, args.json)
    return 0 if res["confirmed"] else 3


def cmd_recompute(args: argparse.Namespace) -> int:
    _print(_app(args).recompute_open_requirements("cli"), args.json)
    return 0


def cmd_withdraw_announcement(args: argparse.Namespace) -> int:
    app = _app(args)
    res = app.withdraw_announcement(args.announcement_id, reason=args.reason or "", by=args.by)
    _print({"id": res["announcement"]["id"], "status": res["announcement"]["status"],
            "review_sync": res["impact"]["review_sync"]}, args.json)
    return 0


def cmd_correct_mapping(args: argparse.Namespace) -> int:
    app = _app(args)
    res = app.correct_mapping(item=args.item, source=args.source, lkey=args.lkey,
                              to_material=args.to, by=args.by, note=args.note or "")
    _print({"listing": res["mapping"].get("material_id"),
            "status": res["mapping"]["status"],
            "affected": [(a["order_id"], a["changed"])
                         for a in res["impact"]["recomputation"]["affected"]]}, args.json)
    return 0


def cmd_approve_substitution(args: argparse.Namespace) -> int:
    params = json.loads(args.param_ranges) if args.param_ranges else {}
    res = _app(args).approve_substitution(
        approval_id=args.id, from_model=args.from_model, to_model=args.to_model,
        equipment_models=args.equipment or [], parameter_ranges=params, by=args.by)
    _print({"approval_id": res["approval"]["id"],
            "equipment_models": res["approval"]["equipment_models"],
            "parameter_ranges": res["approval"]["parameter_ranges"]}, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="procurement",
        description="关键零部件供应采集核验与替代确认系统（仅标准库）")
    # 全局参数放在子命令之前也可用
    parser.add_argument("--state", default=DEFAULT_STATE, help="状态 JSON 路径")
    parser.add_argument("--as-of", default=None, help="核对基准日 YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", default=False, help="输出 JSON")

    # 子命令之后同样可用；SUPPRESS 保证未提供时不覆盖前面解析到的全局值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=argparse.SUPPRESS, help="状态 JSON 路径")
    common.add_argument("--as-of", default=argparse.SUPPRESS, help="核对基准日 YYYY-MM-DD")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    p = add("demo", help="运行内置完整演练场景")
    p.add_argument("--out", default="output")
    p.add_argument("--fresh", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = add("run", help="从 samples + fixtures 构建并出报告")
    p.add_argument("--samples", default="samples")
    p.add_argument("--fixtures", default="fixtures")
    p.add_argument("--out", default="output")
    p.add_argument("--fresh", action="store_true")
    p.set_defaults(func=cmd_run)

    p = add("ingest", help="导入单个文件")
    p.add_argument("kind", choices=["catalog", "announcements", "quotes", "assets", "orders"])
    p.add_argument("path")
    p.add_argument("--source")
    p.add_argument("--adapter")
    p.set_defaults(func=cmd_ingest)

    p = add("collect", help="按来源频率与检查点采集")
    p.add_argument("--config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_collect)

    p = add("check", help="采购前核对订单")
    p.add_argument("order_id")
    p.set_defaults(func=cmd_check)

    p = add("order", help="订单视图")
    p.add_argument("order_id")
    p.set_defaults(func=cmd_order)

    p = add("material", help="物料视图")
    p.add_argument("identifier")
    p.set_defaults(func=cmd_material)

    p = add("reviews", help="待处理复核事项")
    p.set_defaults(func=cmd_reviews)

    p = add("dashboard", help="汇总看板")
    p.set_defaults(func=cmd_dashboard)

    p = add("confirm", help="确认/下达订单（固化原始决策）")
    p.add_argument("order_id")
    p.add_argument("--by", default="buyer")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_confirm)

    p = add("recompute", help="重算所有未下单需求")
    p.set_defaults(func=cmd_recompute)

    p = add("withdraw-announcement", help="撤回公告并重算")
    p.add_argument("announcement_id")
    p.add_argument("--reason", default="")
    p.add_argument("--by", default="governance")
    p.set_defaults(func=cmd_withdraw_announcement)

    p = add("correct-mapping", help="更正物料映射")
    p.add_argument("--item"); p.add_argument("--source"); p.add_argument("--lkey")
    p.add_argument("--to", required=True)
    p.add_argument("--by", default="governance"); p.add_argument("--note")
    p.set_defaults(func=cmd_correct_mapping)

    p = add("approve-substitution", help="批准有条件替代（设备+参数范围）")
    p.add_argument("--id"); p.add_argument("--from-model", required=True)
    p.add_argument("--to-model", required=True)
    p.add_argument("--equipment", nargs="*", default=[])
    p.add_argument("--param-ranges", default="")
    p.add_argument("--by", default="governance")
    p.set_defaults(func=cmd_approve_substitution)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyError as exc:
        print(f"错误：找不到对象 {exc}", file=sys.stderr)
        return 4
    except FileNotFoundError as exc:
        print(f"错误：文件不存在 {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
