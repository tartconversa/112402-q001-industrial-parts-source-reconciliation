"""顶层入口别名：等价于 ``python -m procurement``。

也提供 :func:`build_app` 便于快速构造应用::

    from app import build_app
    app = build_app(as_of="2026-10-20")
    app.ingest_samples("samples")
"""
from __future__ import annotations

from procurement import *  # noqa: F401,F403
from procurement import ProcurementApp, cli_main, __all__ as _PKG_ALL  # noqa: F401

__all__ = list(_PKG_ALL) + ["build_app", "main"]


def build_app(state_path=None, *, as_of=None, **kwargs):
    return ProcurementApp(state_path, as_of=as_of, **kwargs)


def main(argv=None):
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
