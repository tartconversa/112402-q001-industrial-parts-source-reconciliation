"""JSON 文件持久化的状态库。

一个 ``Store`` 持有整个应用状态（来源、检查点、原始版本、物料、映射、公告、
审批、订单、复核事项、审计日志）。写入采用临时文件 + 原子替换，重复运行安全。
``state_path=None`` 时为纯内存模式，便于单元测试隔离。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import dump_json_atomic, iso, load_json, utc_now


def fresh_data() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": iso(utc_now()),
        "sources": {},
        "checkpoints": {},
        "runs": [],
        "raw_records": {},
        "record_versions": [],
        "listings": {},
        "quotes": {},
        "materials": {},
        "assets": {},
        "orders": {},
        "announcements": {},
        "approvals": {},
        "review_items": {},
        "audit": [],
        "recomputations": [],
        "seq": 0,
    }


class Store:
    def __init__(self, state_path: str | Path | None = None):
        self.state_path = Path(state_path) if state_path else None
        if self.state_path and self.state_path.is_dir():
            self.state_path = self.state_path / "state.json"
        self.data: dict[str, Any] = fresh_data()
        if self.state_path and self.state_path.exists():
            self.data = load_json(self.state_path)

    # ---- 持久化 -----------------------------------------------------------
    def save(self) -> None:
        if self.state_path is not None:
            dump_json_atomic(self.state_path, self.data)

    # ---- 序列 / ID --------------------------------------------------------
    def next_seq(self) -> int:
        self.data["seq"] = int(self.data.get("seq", 0)) + 1
        return self.data["seq"]

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{self.next_seq():04d}"

    # ---- 审计 -------------------------------------------------------------
    def audit(self, action: str, *, actor: str = "system", **details: Any) -> dict[str, Any]:
        event = {
            "id": self.new_id("AUD"),
            "at": iso(utc_now()),
            "actor": actor,
            "action": action,
            "details": details,
        }
        self.data["audit"].append(event)
        return event

    # ---- 便捷集合 ---------------------------------------------------------
    @property
    def runs(self) -> list:
        return self.data["runs"]

    @property
    def record_versions(self) -> list:
        return self.data["record_versions"]

    @property
    def raw_records(self) -> dict:
        return self.data["raw_records"]

    @property
    def sources(self) -> dict[str, Any]:
        return self.data["sources"]

    @property
    def checkpoints(self) -> dict[str, Any]:
        return self.data["checkpoints"]

    @property
    def listings(self) -> dict[str, Any]:
        return self.data["listings"]

    @property
    def quotes(self) -> dict[str, Any]:
        return self.data["quotes"]

    @property
    def materials(self) -> dict[str, Any]:
        return self.data["materials"]

    @property
    def assets(self) -> dict[str, Any]:
        return self.data["assets"]

    @property
    def orders(self) -> dict[str, Any]:
        return self.data["orders"]

    @property
    def announcements(self) -> dict[str, Any]:
        return self.data["announcements"]

    @property
    def approvals(self) -> dict[str, Any]:
        return self.data["approvals"]

    @property
    def review_items(self) -> dict[str, Any]:
        return self.data["review_items"]

    @property
    def recomputations(self) -> list[dict[str, Any]]:
        return self.data["recomputations"]

    @property
    def audit_events(self) -> list[dict[str, Any]]:
        return self.data["audit"]
