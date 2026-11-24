"""不同来源字段格式的适配器。

每个来源可以声明自己的 ``adapter`` 与 ``field_map``。适配器只负责把来源行
（CSV/JSON/HTML/EML 解析出的 dict）映射为统一的 canonical observation，
不做任何采购判定。无法识别的列会作为规格参数保留，避免丢失证据。
"""
from __future__ import annotations

import re
from typing import Any

from .common import parse_int, parse_number, parse_qty
from .specs import build_spec

# canonical 商业字段 -> 常见来源表头（小写、去空格/下划线后比较）
HEADER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "source": ("source", "source_id", "dealer", "vendor", "distributor", "来源", "经销商"),
    "item": ("item", "sku", "dealer_sku", "dealer_code", "item_code", "distributor_sku",
             "vendor_sku", "经销商编码", "商品编码", "编码"),
    "name": ("name", "item_name", "product_name", "title", "description", "商品名", "名称"),
    "model": ("model", "mfr_model", "manufacturer_model", "mfr_part", "mfr_part_no",
              "mpn", "part_number", "manufacturer_part_number", "厂商型号", "型号"),
    "manufacturer": ("manufacturer", "mfr", "brand", "maker", "厂商", "品牌"),
    "available": ("available", "stock", "stock_qty", "qty_available", "inventory",
                  "availability", "可用", "库存", "可用量"),
    "price": ("price", "unit_price", "cost", "quote_price", "单价", "价格"),
    "currency": ("currency", "ccy", "币种"),
    "lead_days": ("lead_days", "lead_time_days", "leadtime_days", "leadtime",
                  "delivery_days", "lead_time", "交期", "交货天数"),
    "source_updated": ("updated", "updated_at", "listed_at", "last_updated",
                       "source_updated", "update_time", "更新时间", "来源更新时间"),
    "url": ("url", "link", "page", "source_url", "链接"),
    "quote_id": ("quote_id", "quote_no", "quotation_id", "id", "报价单号"),
    "quoted_at": ("quoted_at", "quote_date", "quotation_date", "as_of", "报价时间", "报价日期"),
    "valid_until": ("valid_until", "expires_at", "expiry", "valid_to", "报价有效期"),
    "min_qty": ("min_qty", "moq", "minimum_order_qty", "起订量"),
}

def _norm_header(h: str) -> str:
    return str(h).strip().lower().replace("-", "_").replace(" ", "_")


# 只有商业/元数据表头需要排除在规格之外（含所有候选别名，如 updated/sku/mpn）。
# 规格列（load_rating/bore_mm/...）保留下来交给 build_spec + FIELD_ALIASES 归一。
_COMMERCIAL_KEYS = (
    {_norm_header(k) for k in HEADER_CANDIDATES}
    | {_norm_header(a) for aliases in HEADER_CANDIDATES.values() for a in aliases}
    | {"adapter", "source_format", "data_feed"}
)


def build_header_index(row: dict[str, Any]) -> dict[str, str]:
    """规范化表头 -> 原始表头。"""
    return {_norm_header(k): k for k in row.keys() if k is not None}


def pick(row: dict[str, Any], index: dict[str, str], canonical: str,
         field_map: dict[str, str] | None = None) -> Any:
    """按 field_map 覆盖 + 候选表头顺序取值。"""
    if field_map and canonical in field_map:
        key = field_map[canonical]
        if key in row:
            return row[key]
        nk = _norm_header(key)
        if nk in index:
            return row[index[nk]]
    for candidate in HEADER_CANDIDATES.get(canonical, (canonical,)):
        ck = _norm_header(candidate)
        if ck in index:
            return row[index[ck]]
    return None


def adapt_offer_row(row: dict[str, Any], *, source_id: str | None = None,
                    adapter: str = "generic",
                    field_map: dict[str, str] | None = None) -> dict[str, Any]:
    """把一条供应商目录/报价行适配为 canonical offer。

    catalog 与 quote_import 共用；报价特有字段存在时一并保留。
    """
    index = build_header_index(row)

    def get(key: str) -> Any:
        return pick(row, index, key, field_map)

    sid = get("source") or source_id
    item = get("item")
    model = get("model")
    name = get("name")
    manufacturer = get("manufacturer")

    # 其余未被识别为商业字段的列，作为规格参数/尺寸输入。
    # 同时排除本次 field_map 明确映射给 canonical 商业字段的原始表头。
    mapped_headers = {_norm_header(v) for v in (field_map or {}).values()}
    extra_spec: dict[str, Any] = {}
    for norm_k, original_k in index.items():
        if norm_k in _COMMERCIAL_KEYS or norm_k in mapped_headers:
            continue
        value = row[original_k]
        if value not in (None, ""):
            extra_spec[norm_k] = value

    spec = build_spec(model=model, manufacturer=manufacturer, name=name, **extra_spec)

    available = parse_qty(get("available"))
    price = parse_number(get("price"))
    lead_days = parse_int(get("lead_days"))
    min_qty = parse_int(get("min_qty"), default=1)

    return {
        "source_id": str(sid) if sid not in (None, "") else None,
        "item": str(item).strip() if item not in (None, "") else None,
        "name": str(name).strip() if name not in (None, "") else None,
        "manufacturer": spec["manufacturer"],
        "model": spec["model"],
        "spec": spec,
        "available": available,
        "price": price,
        "currency": (str(get("currency")).strip() if get("currency") not in (None, "") else "CNY"),
        "lead_days": lead_days,
        "min_qty": min_qty or 1,
        "source_updated": (str(get("source_updated")).strip()
                          if get("source_updated") not in (None, "") else None),
        "url": (str(get("url")).strip() if get("url") not in (None, "") else None),
        # 报价特有
        "quote_id": (str(get("quote_id")).strip() if get("quote_id") not in (None, "") else None),
        "quoted_at_raw": get("quoted_at"),
        "valid_until_raw": get("valid_until"),
        "adapter": adapter,
    }


# 具名适配器预设：演示“不同来源不同字段格式”
ADAPTER_PRESETS: dict[str, dict[str, str]] = {
    "sample_catalog": {},
    "dealer_wide": {
        "item": "distributor_sku",
        "model": "mfr_part_no",
        "manufacturer": "mfr_name",
        "available": "stock_qty",
        "price": "unit_price",
        "lead_days": "lead_time_days",
        "source_updated": "listed_at",
    },
    "quote_standard": {
        "item": "dealer_sku",
        "model": "mfr_part_number",
        "quoted_at": "quote_date",
    },
}


def adapt_row(row: dict[str, Any], *, source_id: str | None = None,
              adapter: str = "generic",
              field_map: dict[str, str] | None = None) -> dict[str, Any]:
    merged_map = dict(ADAPTER_PRESETS.get(adapter, {}))
    if field_map:
        merged_map.update(field_map)
    return adapt_offer_row(row, source_id=source_id, adapter=adapter, field_map=merged_map)


# ---------------------------------------------------------------------------
# 公告适配
# ---------------------------------------------------------------------------

ANN_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "announcement_id": ("announcement_id", "id", "notice_id", "公告编号"),
    "manufacturer": ("manufacturer", "mfr", "brand", "厂商"),
    "model": ("model", "mfr_model", "part_number", "mpn", "mfr_part_no", "厂商型号", "型号"),
    "kind": ("kind", "type", "category", "notice_type", "公告类型", "类型"),
    "title": ("title", "subject", "headline", "标题"),
    "body": ("body", "content", "text", "description", "summary", "正文", "内容"),
    "url": ("url", "link", "source_url", "链接"),
    "published_at": ("published_at", "publish_date", "date", "issued_at", "发布日期", "发布时间"),
    "effective_at": ("effective_at", "effective_date", "effective", "生效日期"),
    "last_order_date": ("last_order_date", "last_buy_date", "final_order_date", "末次下单日"),
    "replacement_model": ("replacement_model", "replacement", "successor",
                          "cross_reference", "替代型号"),
    "lead_days": ("lead_days", "new_lead_days", "revised_lead_time_days", "new_lead_time",
                  "交期天数", "新交期天数", "交期(天)", "交期"),
    "earliest_delivery_date": ("earliest_delivery_date", "new_delivery_date",
                               "expected_availability", "expected_date", "预计交付"),
    "equipment_models": ("equipment_models", "equipment_model", "device_models",
                         "applicable_equipment", "设备型号"),
    "parameter_ranges": ("parameter_ranges", "approved_ranges", "param_ranges", "参数范围"),
}

KIND_MAP = {
    "eol": "discontinuation", "discontinued": "discontinuation",
    "stop_sale": "discontinuation", "discontinuation": "discontinuation",
    "停产": "discontinuation", "停产通知": "discontinuation",
    "replacement": "replacement", "cross_reference": "replacement",
    "supersession": "replacement", "替代": "replacement", "兼容替代": "replacement",
    "lead_time": "lead_time", "delivery": "lead_time", "leadtime": "lead_time",
    "交期": "lead_time", "交付": "lead_time", "交期变更": "lead_time",
    "compatibility": "compatibility", "compat": "compatibility", "兼容": "compatibility",
}


def adapt_announcement_row(row: dict[str, Any], *, source_id: str | None = None,
                           field_map: dict[str, str] | None = None) -> dict[str, Any]:
    index = build_header_index(row)

    def get(key: str) -> Any:
        if field_map and key in field_map:
            fk = field_map[key]
            if fk in row:
                return row[fk]
        for candidate in ANN_FIELD_CANDIDATES.get(key, (key,)):
            ck = _norm_header(candidate)
            if ck in index:
                return row[index[ck]]
        return None

    raw_kind = get("kind")
    kind = KIND_MAP.get(_norm_header(str(raw_kind or "")), None)
    if kind is None and raw_kind:
        low = str(raw_kind).strip().lower()
        kind = KIND_MAP.get(low)
        if kind is None:
            # 关键词推断
            if any(w in str(raw_kind) for w in ("停产", "停止", "EOL")):
                kind = "discontinuation"
            elif any(w in str(raw_kind) for w in ("替代", "兼容")):
                kind = "replacement"
            elif any(w in str(raw_kind) for w in ("交期", "交付")):
                kind = "lead_time"
            else:
                kind = "compatibility"

    equip = get("equipment_models")
    if isinstance(equip, str):
        equip_list = [x.strip() for x in equip.replace("，", ",").replace(";", ",").split(",") if x.strip()]
    elif isinstance(equip, (list, tuple)):
        equip_list = list(equip)
    else:
        equip_list = []

    pranges = get("parameter_ranges")
    if isinstance(pranges, str) and pranges.strip():
        import json
        try:
            pranges = json.loads(pranges)
        except ValueError:
            pranges = {"_text": pranges.strip()}

    title = get("title")
    body = get("body")
    text_blob = " ".join(str(x) for x in (title, body) if x)

    model = get("model")
    replacement_model = get("replacement_model")
    lead_days = parse_int(get("lead_days"))
    # 从自由文本（邮件正文/HTML）中补抽型号、替代型号与交期
    if text_blob:
        code_pat = r"\b[A-Za-z]{1,4}[-]?\d{2,5}(?:[-/][A-Za-z0-9]+)?\b"
        codes = re.findall(code_pat, text_blob)
        if not model and codes:
            model = codes[0]
        if not replacement_model:
            m = re.search(r"(?:替代|替换|successor|replace(?:s|ment)?(?: to)?)\s*[:：]?\s*("
                          + code_pat + r")", text_blob, re.IGNORECASE)
            if m:
                replacement_model = m.group(1)
        if lead_days is None:
            m = re.search(r"(\d{1,3})\s*(?:个?工作日|天|日|days?)", text_blob, re.IGNORECASE)
            if m and ("交期" in text_blob or "lead" in text_blob.lower()
                      or "delivery" in text_blob.lower() or "周期" in text_blob):
                lead_days = int(m.group(1))
        if kind is None:
            if any(w in text_blob for w in ("停产", "停止生产", "EOL", "end of life")):
                kind = "discontinuation"
            elif any(w in text_blob for w in ("替代", "兼容", "cross")):
                kind = "replacement"
            elif any(w in text_blob for w in ("交期", "交付", "lead time", "delivery")):
                kind = "lead_time"

    return {
        "source_id": source_id or get("source"),
        "announcement_id": get("announcement_id"),
        "manufacturer": get("manufacturer"),
        "model": model,
        "kind": kind,
        "title": title,
        "body": body,
        "url": get("url"),
        "published_at_raw": get("published_at"),
        "effective_at_raw": get("effective_at"),
        "last_order_date_raw": get("last_order_date"),
        "replacement_model": replacement_model,
        "lead_days": lead_days,
        "earliest_delivery_date_raw": get("earliest_delivery_date"),
        "equipment_models_hint": equip_list,
        "parameter_ranges_hint": pranges if isinstance(pranges, dict) else None,
        "raw_row": dict(row),
    }
