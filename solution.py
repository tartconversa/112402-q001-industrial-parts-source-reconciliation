"""兼容性模块名别名，内容与 :mod:`procurement` 公共 API 一致。"""
from procurement import *  # noqa: F401,F403
from procurement import ProcurementApp, cli_main  # noqa: F401


def build_app(state_path=None, *, as_of=None, **kwargs):
    return ProcurementApp(state_path, as_of=as_of, **kwargs)


def main(argv=None):
    return cli_main(argv)
