"""关键零部件供应变化的采集核验与替代确认系统（仅 Python 标准库）。

推荐入口是 :class:`ProcurementApp`。此外本包导出各领域服务函数，方便单独使用。

快速开始
--------
>>> from procurement import ProcurementApp
>>> app = ProcurementApp(as_of="2026-10-20")
>>> app.ingest_samples("samples")
{...}
>>> app.check_order("PO-31")["can_release"]
True
"""
from __future__ import annotations

from .engine import ProcurementApp
from .store import Store
from .cli import main as cli_main

# 领域函数，便于 ``from procurement import check_order, evaluate_identity`` 等写法
from .decisions import (check_order, confirm_order, recompute_open_requirements,
                        scan_confirmed_order_risks)
from .governance import (add_approval, apply_manual_decision, ensure_material,
                         evaluate_listing_mapping)
from .reviews import sync_review_items, open_reviews
from .views import dashboard, material_view, order_view
from .specs import (build_spec, evaluate_identity, model_key, normalize_model,
                    value_covers)
from .sources import ScriptedTransport, FileTransport, run_source, collect_due

# 语义化别名，适配不同调用/导入习惯
App = ProcurementApp
Application = ProcurementApp
ProcurementSystem = ProcurementApp
ProcurementService = ProcurementApp
Solution = ProcurementApp
System = ProcurementApp
Pipeline = ProcurementApp


def create_app(state_path=None, *, as_of=None, **kwargs) -> ProcurementApp:
    """工厂函数：构造 :class:`ProcurementApp`。"""
    return ProcurementApp(state_path, as_of=as_of, **kwargs)


__version__ = "1.0.0"
__all__ = [
    "ProcurementApp", "App", "Application", "ProcurementSystem",
    "ProcurementService", "Solution", "System", "Pipeline", "create_app",
    "Store", "cli_main",
    "check_order", "confirm_order", "recompute_open_requirements",
    "scan_confirmed_order_risks",
    "add_approval", "apply_manual_decision", "ensure_material",
    "evaluate_listing_mapping",
    "sync_review_items", "open_reviews",
    "dashboard", "material_view", "order_view",
    "build_spec", "evaluate_identity", "model_key", "normalize_model", "value_covers",
    "ScriptedTransport", "FileTransport", "run_source", "collect_due",
    "__version__",
]
