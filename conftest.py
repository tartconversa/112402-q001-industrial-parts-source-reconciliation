"""Pytest 引导：确保仓库根目录在 ``sys.path`` 上。

对 ``python -m unittest discover -s tests`` 无副作用；仅为某些从其他工作目录
启动 pytest / IDE 的场景保证 ``import procurement`` / ``import app`` 可用。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
