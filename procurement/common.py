"""共享基础工具：异常、时间、哈希、ID 与原子 JSON 读写。

仅使用 Python 3 标准库。所有对外结构尽量使用可直接 JSON 序列化的
``dict`` / ``list`` / 字符串，确保证据链与报告可以落盘、复核。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# 东八区（样本时间使用 +08:00）。naive 时间默认按该时区解释。
DEFAULT_TZ = timezone(timedelta(hours=8))

ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f%z"


class ProcurementError(Exception):
    """业务错误基类。"""


class SourceUnavailable(ProcurementError):
    """来源短时不可用（网络错误 / 输入缺失等），应保留检查点稍后续采。"""

    def __init__(self, source_id: str, reason: str = "", *, retry_after: float | None = None):
        self.source_id = source_id
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(f"source {source_id} unavailable: {reason}")


class CheckpointRequired(ProcurementError):
    """检查点状态异常。"""


class GovernanceError(ProcurementError):
    """物料治理操作不合法。"""


# ---------------------------------------------------------------------------
# 时间处理
# ---------------------------------------------------------------------------

def parse_datetime(value: Any, *, default_tz: timezone = DEFAULT_TZ) -> datetime | None:
    """宽容解析 ISO-8601 日期/时间，统一返回带时区的 UTC datetime。

    - 空值 -> None
    - 纯日期（``2026-11-02``）-> 当日 00:00，按 ``default_tz`` 解释
    - naive datetime -> 按 ``default_tz`` 附加时区
    - 带偏移量 -> 转换为 UTC
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        # 结尾 Z
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            # 兼容 "2026/10/14 09:00" 等格式
            cleaned = text.replace("/", "-")
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ProcurementError(f"无法解析时间: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def parse_date(value: Any) -> date | None:
    """解析为 :class:`date`；日期时间取其在默认时区下的日历日。"""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    dt = parse_datetime(value)
    assert dt is not None
    return dt.astimezone(DEFAULT_TZ).date()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def as_date(value: Any, *, today: date | None = None) -> date | None:
    """把 datetime/字符串/date 统一成 date；None 透传。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(DEFAULT_TZ).date() if value.tzinfo else value.date()
    return parse_date(value)


def add_days(day: date | None, days: float | int | None) -> date | None:
    if day is None or days in (None, ""):
        return None
    return day + timedelta(days=float(days))


# ---------------------------------------------------------------------------
# 哈希 / ID
# ---------------------------------------------------------------------------

def sha256_text(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def content_hash(payload: Any) -> str:
    """对任意 JSON 可序列化内容生成稳定哈希（sort_keys）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(blob)


def short_hash(*parts: Any, length: int = 12) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return sha256_text(raw)[:length]


def deterministic_id(prefix: str, *parts: Any, length: int = 10) -> str:
    return f"{prefix}_{short_hash(*parts, length=length)}"


_slug_re = re.compile(r"[^A-Za-z0-9]+")


def slug(value: str | None, *, upper: bool = True) -> str:
    text = _slug_re.sub("", str(value or ""))
    return text.upper() if upper else text.lower()


# ---------------------------------------------------------------------------
# 数值解析
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_number(text: Any) -> float | None:
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM_RE.search(str(text).replace(",", ""))
    return float(m.group(0)) if m else None


def parse_int(text: Any, default: int | None = None) -> int | None:
    num = parse_number(text)
    if num is None:
        return default
    return int(num)


def parse_qty(text: Any) -> int | None:
    """库存/数量：空单元格 => None（来源未提供），否则取整数。"""
    if text is None or text == "":
        return None
    if isinstance(text, bool):  # bool 是 int 子类，需先处理
        return 1 if text else 0
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().lower()
    if s in ("in stock", "instock", "yes", "有货", "充足"):
        return None  # 有货但数量未知
    if s in ("out of stock", "oos", "no", "无货", "缺货", "0"):
        return 0
    val = parse_int(text)
    return val


# ---------------------------------------------------------------------------
# JSON 持久化
# ---------------------------------------------------------------------------

def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json_atomic(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_csv_dicts(path: str | Path) -> list[dict[str, str]]:
    import csv

    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def write_json_report(path: str | Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json_atomic(path, data)
    return path


def chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
