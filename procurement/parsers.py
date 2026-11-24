"""把 CSV / JSON / HTML 页面 / EML 邮件附件解析成原始 dict 记录。

采集应用面对“网页和邮件附件”，这里用标准库提供离线、可重复的解析能力。
HTML 支持两类常见结构：``<table>`` 表头行，以及带 ``data-field`` /
``data-*`` 属性的公告卡片。EML 会优先解析其中的 CSV/JSON 附件，否则把
主题/正文封装成一条公告记录。
"""
from __future__ import annotations

import csv
import email
import json
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .common import read_csv_dicts


def parse_csv(path: str | Path) -> list[dict[str, str]]:
    return read_csv_dicts(path)


def parse_json(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("records", "items", "data", "announcements", "quotes"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return [data]
    return []


class _TableAndCardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._is_header_cell = False
        self.cards: list[dict[str, str]] = []
        self._card: dict[str, str] | None = None
        self._card_field: str | None = None
        self._card_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "table":
            self._in_table = True
            self._rows = []
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._row is not None and tag in ("td", "th"):
            self._cell = []
            self._is_header_cell = tag == "th"
        elif tag in ("article", "div", "section") and ("data-announcement" in ad or "data-record" in ad):
            self._card = {k[5:].replace("_", "-"): v for k, v in ad.items() if k.startswith("data-")}
        elif self._card is not None and "data-field" in ad:
            self._card_field = ad["data-field"]
            self._card_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(self._row)
            self._row = None
        elif tag == "table":
            if self._rows:
                self.tables.append(self._rows)
            self._in_table = False
        elif self._card_field is not None and tag in ("span", "p", "div", "td", "dd"):
            self._card[self._card_field] = " ".join("".join(self._card_buffer).split())
            self._card_field = None
            self._card_buffer = []
        elif tag in ("article", "div", "section") and self._card is not None and self._card_field is None:
            # 仅在确实收集到字段时保留
            if any(k for k in self._card if not k.startswith("announcement") or self._card.get(k)):
                if len(self._card) > 1:
                    self.cards.append(self._card)
            self._card = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._card_field is not None:
            self._card_buffer.append(data)


def parse_html(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    parser = _TableAndCardParser()
    parser.feed(text)
    records: list[dict[str, Any]] = []
    for rows in parser.tables:
        if not rows:
            continue
        width = max(len(r) for r in rows)
        header = [h or f"col_{i}" for i, h in enumerate(rows[0])]
        for r in rows[1:]:
            if not any(c.strip() for c in r):
                continue
            padded = r + [""] * (width - len(r))
            records.append({header[i]: padded[i] for i in range(len(header))})
    records.extend(parser.cards)
    return records


def parse_eml(path: str | Path) -> list[dict[str, Any]]:
    msg = email.message_from_bytes(Path(path).read_bytes(), policy=policy.default)
    records: list[dict[str, Any]] = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if filename.lower().endswith(".csv"):
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            reader = csv.DictReader(text.splitlines())
            records.extend(dict(r) for r in reader)
        elif filename.lower().endswith(".json"):
            data = json.loads(payload.decode("utf-8", errors="replace"))
            if isinstance(data, list):
                records.extend(r for r in data if isinstance(r, dict))
    if records:
        return records
    # 没有结构化附件：把邮件本身作为一条公告线索
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_content()
                break
    else:
        body = msg.get_content()
    return [{
        "announcement_id": msg.get("Message-ID", "").strip("<>") or None,
        "published_at": msg.get("Date"),
        "title": msg.get("Subject"),
        "body": body,
        "source_format": "eml",
    }]


def parse_file(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return parse_csv(path)
    if suffix == ".json":
        return parse_json(path)
    if suffix in (".html", ".htm"):
        return parse_html(path)
    if suffix == ".eml":
        return parse_eml(path)
    if suffix in (".txt", ""):
        # 尝试 JSON，否则按 CSV
        try:
            return parse_json(path)
        except (json.JSONDecodeError, ValueError):
            return parse_csv(path)
    raise ValueError(f"不支持的输入格式: {path}")


def list_input_files(path: str | Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    allowed = {".csv", ".json", ".html", ".htm", ".eml", ".txt"}
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in allowed)
