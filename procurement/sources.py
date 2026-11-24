"""来源访问频率、断点位置与可恢复采集。

* 每个来源配置独立的 ``frequency_seconds``（或 ``frequency`` 名称）。
* :class:`Checkpoint` 以已提交的 ``offset`` 为核心；每处理完一批即持久化。
* 来源短时不可用抛 :class:`SourceUnavailable`，异常时**不推进** offset，
  下次运行从同一位置续采。
* :class:`FileTransport` 从本地 CSV/JSON/HTML/EML 读取（代表离线拿到的网页
  或邮件附件）；测试可用 :class:`ScriptedTransport` 模拟分页与临时故障。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from .common import SourceUnavailable, iso, parse_datetime, utc_now
from .parsers import parse_file

FREQUENCIES = {
    "realtime": 60,
    "hourly": 3600,
    "every_4h": 4 * 3600,
    "twice_daily": 12 * 3600,
    "daily": 86400,
    "weekly": 7 * 86400,
}


def frequency_seconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return FREQUENCIES.get(str(value).strip().lower())


def is_due(source_cfg: dict[str, Any], checkpoint: dict[str, Any], *, now=None) -> bool:
    freq = frequency_seconds(source_cfg.get("frequency_seconds") or source_cfg.get("frequency"))
    if not freq:
        return True
    last = parse_datetime(checkpoint.get("last_attempt_at"))
    if last is None:
        return True
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=last.tzinfo)
    return (current - last).total_seconds() >= freq


def fresh_checkpoint(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "offset": 0,
        "done": False,
        "cursor": None,
        "status": "new",
        "position_description": "尚未采集",
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_run_id": None,
        "next_allowed_at": None,
    }


@dataclass
class Pull:
    records: list[dict[str, Any]]
    next_offset: int
    done: bool
    cursor: Any = None
    position_description: str | None = None


class Transport(Protocol):
    def pull_batch(self, source_cfg: dict[str, Any], offset: int, batch_size: int) -> Pull: ...


class FileTransport:
    """从来源配置的 ``path`` 读取文件并按 offset 分批。"""

    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else None
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def _resolve(self, raw_path: str) -> Path:
        p = Path(raw_path)
        if not p.is_absolute() and self.base_dir:
            p = self.base_dir / p
        return p

    def records_for(self, source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        path = self._resolve(source_cfg["path"])
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in self._cache:
            if not path.exists():
                raise SourceUnavailable(source_cfg["id"], f"输入不存在: {path}")
            try:
                self._cache[key] = parse_file(path)
            except Exception as exc:  # 解析失败也视为来源暂时不可处理
                raise SourceUnavailable(source_cfg["id"], f"解析失败: {exc}") from exc
        return self._cache[key]

    def pull_batch(self, source_cfg, offset, batch_size) -> Pull:
        records = self.records_for(source_cfg)
        batch = records[offset:offset + batch_size]
        next_offset = offset + len(batch)
        done = next_offset >= len(records)
        return Pull(batch, next_offset, done,
                    position_description=f"行 {next_offset}/{len(records)}")


class ScriptedTransport:
    """按预置记录/分页提供数据，可注入临时故障，供断点续采测试。

    * ``pages[source_id]`` 可以是记录列表（按 batch_size 自动切），
      也可以是 ``list[list[dict]]`` 显式分页。
    * ``fail_at[source_id] = n`` 表示在第 n 次 pull（从 1 开始）抛不可用；
      之后调用恢复正常（模拟“短时”不可用）。
    * ``always_unavailable`` 可模拟持续不可用。
    """

    def __init__(self, pages: dict[str, Any] | None = None, *,
                 fail_at: dict[str, int] | None = None,
                 unavailable_times: dict[str, int] | None = None):
        self.pages = pages or {}
        self.fail_at = fail_at or {}
        self.unavailable_times = unavailable_times or {}
        self.calls: dict[str, int] = {}

    def add(self, source_id: str, records: list[dict[str, Any]]) -> "ScriptedTransport":
        self.pages[source_id] = records
        return self

    def pull_batch(self, source_cfg, offset, batch_size) -> Pull:
        sid = source_cfg["id"]
        self.calls[sid] = self.calls.get(sid, 0) + 1
        call_no = self.calls[sid]
        if sid in self.fail_at and call_no == self.fail_at[sid]:
            raise SourceUnavailable(sid, "scripted temporary outage")
        remaining_failures = self.unavailable_times.get(sid, 0)
        if remaining_failures > 0:
            self.unavailable_times[sid] = remaining_failures - 1
            raise SourceUnavailable(sid, "scripted outage")

        data = self.pages.get(sid, [])
        # 显式分页
        if data and isinstance(data[0], list):
            page_index = offset
            if page_index >= len(data):
                return Pull([], offset, True)
            batch = data[page_index]
            next_offset = offset + 1
            done = next_offset >= len(data)
            return Pull(batch, next_offset, done, cursor=next_offset)

        batch = data[offset:offset + batch_size]
        next_offset = offset + len(batch)
        done = next_offset >= len(data)
        return Pull(batch, next_offset, done)


class Sink(Protocol):
    def persist_batch(self, *, source_id: str, records: list[dict[str, Any]],
                      run_id: str, fetched_at: str, adapter: str | None,
                      field_map: dict[str, str] | None) -> dict[str, Any]: ...


def run_source(store, source_id: str, transport: Transport, sink: Sink, *,
               now=None, batch_size: int | None = None) -> dict[str, Any]:
    """运行单个来源直到完成或不可用；返回 run 报告。检查点逐批提交。"""
    if source_id not in store.sources:
        raise KeyError(f"未注册来源: {source_id}")
    cfg = store.sources[source_id]
    cp = store.checkpoints.setdefault(source_id, fresh_checkpoint(source_id))
    current = now or utc_now()
    fetched_at = iso(current)

    # 上一轮已成功完成 => 新一轮从头扫描以发现变更；sink 幂等。
    start_offset = int(cp.get("offset", 0))
    if cp.get("status") == "success" and cp.get("done"):
        start_offset = 0

    run_id = store.new_id("RUN")
    run = {
        "run_id": run_id,
        "source_id": source_id,
        "kind": cfg.get("kind"),
        "started_at": fetched_at,
        "finished_at": None,
        "status": "running",
        "start_offset": start_offset,
        "end_offset": start_offset,
        "batches": 0,
        "records_seen": 0,
        "new_versions": 0,
        "error": None,
    }
    store.data["runs"].append(run)

    cp["last_run_id"] = run_id
    cp["last_attempt_at"] = fetched_at
    cp["status"] = "in_progress"
    cp["offset"] = start_offset
    cp["done"] = False
    store.save()

    size = batch_size or int(cfg.get("batch_size", 50))
    offset = start_offset
    try:
        while True:
            pull = transport.pull_batch(cfg, offset, size)
            result = sink.persist_batch(
                source_id=source_id, records=pull.records, run_id=run_id,
                fetched_at=fetched_at, adapter=cfg.get("adapter"),
                field_map=cfg.get("field_map"))
            run["new_versions"] += int(result.get("new_versions", 0))
            run["records_seen"] += len(pull.records)
            run["batches"] += 1
            offset = pull.next_offset
            cp["offset"] = offset
            cp["cursor"] = pull.cursor
            cp["done"] = pull.done
            cp["position_description"] = pull.position_description or f"offset={offset}"
            cp["status"] = "in_progress" if not pull.done else "finishing"
            store.save()  # 每批提交检查点
            if pull.done or not pull.records:
                break

        run["status"] = "success"
        run["finished_at"] = iso(now or utc_now())
        run["end_offset"] = offset
        cp["status"] = "success"
        cp["done"] = True
        cp["offset"] = offset
        cp["last_success_at"] = run["finished_at"]
        cp["last_error"] = None
        freq = frequency_seconds(cfg.get("frequency_seconds") or cfg.get("frequency"))
        if freq:
            from datetime import timedelta
            cp["next_allowed_at"] = iso(current + timedelta(seconds=freq))
        store.audit("source.completed", source=source_id, run_id=run_id,
                   records=run["records_seen"], new_versions=run["new_versions"])
        store.save()
        return run
    except SourceUnavailable as exc:
        run["status"] = "unavailable"
        run["error"] = exc.reason
        run["finished_at"] = iso(now or utc_now())
        run["end_offset"] = offset
        cp["status"] = "unavailable"
        cp["last_error"] = exc.reason
        cp["offset"] = offset  # 关键：保留未完成位置
        cp["done"] = False
        if exc.retry_after:
            from datetime import timedelta
            cp["next_allowed_at"] = iso(utc_now() + timedelta(seconds=exc.retry_after))
        store.audit("source.unavailable", source=source_id, run_id=run_id,
                    offset=offset, reason=exc.reason)
        store.save()
        return run
    except Exception as exc:  # noqa: BLE001 - 采集循环需要兜底并保留位置
        run["status"] = "error"
        run["error"] = f"{type(exc).__name__}: {exc}"
        run["finished_at"] = iso(now or utc_now())
        run["end_offset"] = offset
        cp["status"] = "error"
        cp["last_error"] = run["error"]
        cp["offset"] = offset
        cp["done"] = False
        store.audit("source.error", source=source_id, run_id=run_id, error=run["error"])
        store.save()
        return run


def collect_due(store, transport: Transport, sink: Sink, *, now=None,
                force: bool = False, only: list[str] | None = None) -> list[dict[str, Any]]:
    """按频率运行所有到期来源。``force=True`` 忽略频率。"""
    reports: list[dict[str, Any]] = []
    for sid, cfg in list(store.sources.items()):
        if only and sid not in only:
            continue
        if not cfg.get("enabled", True):
            continue
        cp = store.checkpoints.get(sid, fresh_checkpoint(sid))
        if not force and not is_due(cfg, cp, now=now):
            reports.append({"run_id": None, "source_id": sid, "status": "skipped_not_due",
                            "frequency_seconds": frequency_seconds(
                                cfg.get("frequency_seconds") or cfg.get("frequency"))})
            continue
        reports.append(run_source(store, sid, transport, sink, now=now))
    return reports
