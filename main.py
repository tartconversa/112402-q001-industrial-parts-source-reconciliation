"""可执行入口别名：``python main.py ...`` 等价于 ``python -m procurement ...``。"""
from app import build_app, main  # noqa: F401
from procurement import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(main())
