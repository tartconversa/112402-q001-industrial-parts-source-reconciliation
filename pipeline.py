"""流水线式入口别名。

提供 :func:`run_pipeline` 一次性完成 samples/fixtures 采集、核验与报告。
"""
from __future__ import annotations

from pathlib import Path

from procurement import *  # noqa: F401,F403
from procurement import ProcurementApp  # noqa: F401
from procurement.reporting import build_report, write_reports


def run_pipeline(*, samples: str = "samples", fixtures: str | None = "fixtures",
                 state_path: str = "output/state.json", outdir: str = "output",
                 as_of: str | None = "2026-10-20", fresh: bool = True) -> dict:
    """可重复运行的端到端流水线，返回报告 dict 并落盘 JSON/Markdown。"""
    state = Path(state_path)
    if fresh and state.exists():
        state.unlink()
    app = ProcurementApp(state, as_of=as_of)
    summary = {}
    if Path(samples).exists():
        summary["samples"] = app.ingest_samples(samples)
    if fixtures and Path(fixtures).exists():
        from procurement.demo import load_fixtures_into
        summary["fixtures"] = load_fixtures_into(app, fixtures)
    app.refresh("pipeline")
    paths = write_reports(app, outdir)
    return {"summary": summary, "paths": {k: str(v) for k, v in paths.items()},
            "report": build_report(app)}


def main(argv=None):
    from procurement.cli import main as _main
    return _main(argv or ["run"])


if __name__ == "__main__":
    raise SystemExit(main())
