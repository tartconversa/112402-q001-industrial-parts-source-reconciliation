"""厂商型号规范化、参数建模与保守比对。

设计原则（对应题目要求）：

* 厂商编码在去除 ``-``/``_``/空白/点 等分隔符后比较，但**保留大小写**——
  README 明确“型号区分大小写”，因此大小写不同只产生 ``model_case_mismatch``
  待核，而不会被自动合并。
* 商品名相似度始终计算并展示，但**永远不能**作为确认同一物料或放行替代的证据。
* 额定参数/尺寸只要双方都提供且互斥，即判为冲突；只在一侧提供则信息不足。
* 只有“编码关系可靠 + 至少一项相互匹配的尺寸/额定参数 + 无冲突”才自动确认。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable

from .status import LOAD_LEVELS

# 字段别名 -> 规范属性名。键统一为小写、下划线形式。
FIELD_ALIASES: dict[str, tuple[str, str]] = {
    # 额定参数
    "load": ("param", "load"), "load_rating": ("param", "load"),
    "rated_load": ("param", "load"), "loadrating": ("param", "load"),
    "rating": ("param", "load"), "负载": ("param", "load"),
    "额定负载": ("param", "load"), "额定载荷": ("param", "load"),
    "load_capacity": ("param", "load"),
    "speed": ("param", "speed"), "max_speed": ("param", "speed"),
    "转速": ("param", "speed"),
    "voltage": ("param", "voltage"), "rated_voltage": ("param", "voltage"),
    "电压": ("param", "voltage"),
    "temperature": ("param", "temperature_range"), "temp_range": ("param", "temperature_range"),
    "精度等级": ("param", "precision"), "precision": ("param", "precision"),
    # 尺寸
    "bore": ("dim", "bore_mm"), "bore_mm": ("dim", "bore_mm"),
    "inner_diameter": ("dim", "bore_mm"), "id_mm": ("dim", "bore_mm"),
    "d": ("dim", "bore_mm"), "内径": ("dim", "bore_mm"),
    "outer_diameter": ("dim", "outer_diameter_mm"), "od": ("dim", "outer_diameter_mm"),
    "od_mm": ("dim", "outer_diameter_mm"), "outer_diameter_mm": ("dim", "outer_diameter_mm"),
    "diameter_outer": ("dim", "outer_diameter_mm"), "外径": ("dim", "outer_diameter_mm"),
    "width": ("dim", "width_mm"), "width_mm": ("dim", "width_mm"),
    "breadth": ("dim", "width_mm"), "b": ("dim", "width_mm"),
    "厚度": ("dim", "width_mm"), "宽度": ("dim", "width_mm"),
    "length_mm": ("dim", "length_mm"), "length": ("dim", "length_mm"),
    "长度": ("dim", "length_mm"),
}

# 单位归一化到基准单位
_UNIT_FACTORS: dict[str, tuple[str, float]] = {
    "mm": ("length", 1.0), "millimeter": ("length", 1.0), "millimeters": ("length", 1.0),
    "cm": ("length", 10.0), "m": ("length", 1000.0), "meter": ("length", 1000.0),
    "in": ("length", 25.4), "inch": ("length", 25.4), "inches": ("length", 25.4),
    "n": ("force", 1.0), "kn": ("force", 1000.0),
    "v": ("voltage", 1.0), "kv": ("voltage", 1000.0),
}

_SEP_RE = re.compile(r"[\s\-_./\\]+")
_TOKEN_SPLIT_RE = re.compile(r"[_\-\s]+")
_NUM_UNIT_RE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*([A-Za-zμµ°/]+)?\s*$")
_DIM_TRIPLE_RE = re.compile(
    r"^\s*([-+]?\d+(?:\.\d+)?)\s*[x×*]\s*([-+]?\d+(?:\.\d+)?)\s*[x×*]\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def model_key(model: Any) -> str | None:
    """去除分隔符但保留大小写的厂商编码键。"""
    if model is None:
        return None
    text = str(model).strip()
    if not text:
        return None
    return _SEP_RE.sub("", text)


def model_fold_key(model: Any) -> str | None:
    """进一步大写折叠，仅用于发现大小写差异，不用于确认。"""
    key = model_key(model)
    return key.upper() if key else None


# 常见命名别名
normalize_model = model_key
normalize_model_code = model_key
model_signature = model_key


def name_similarity(a: Any, b: Any) -> float:
    if not a or not b:
        return 0.0
    return round(SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio(), 4)


def model_relation(a_model: Any, b_model: Any) -> str:
    """返回 exact / normalized_code / case_only / similar / different / missing。"""
    if not a_model or not b_model:
        return "missing"
    a_s, b_s = str(a_model).strip(), str(b_model).strip()
    if a_s == b_s:
        return "exact"
    ka, kb = model_key(a_s), model_key(b_s)
    if ka and kb and ka == kb:
        return "normalized_code"
    fa, fb = model_fold_key(a_s), model_fold_key(b_s)
    if fa and fb and fa == fb:
        return "case_only"
    if name_similarity(a_s, b_s) >= 0.75:
        return "similar"
    return "different"


def infer_category(model: str | None, name: str | None = None) -> str | None:
    text = f"{model or ''} {name or ''}".upper()
    if re.search(r"\bBR|BEAR|轴承|62\d{2}|63\d{2}", text):
        return "bearing"
    if re.search(r"\bCT|CTRL|控制器|PLC|DRIVE", text):
        return "controller"
    return None


def _canonical_load_level(raw: str) -> tuple[str, int] | None:
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    key = re.sub(r"_+", "_", key)
    if key in LOAD_LEVELS:
        return key, LOAD_LEVELS[key]
    # 中文直接匹配
    for label, level in LOAD_LEVELS.items():
        if any(ord(ch) > 127 for ch in label) and label in raw:
            return label, level
    if raw.strip() in LOAD_LEVELS:
        return raw.strip(), LOAD_LEVELS[raw.strip()]
    return None


def normalize_attribute(name: str, value: Any) -> dict[str, Any] | None:
    """把任意来源的属性值规范化为可比较结构。空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, dict) and "kind" in value:
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw == "":
            return None
    else:
        raw = value

    canon = name
    if canon == "load":
        # 可能是等级词，也可能是 "28.5 kN"
        if isinstance(raw, str):
            lvl = _canonical_load_level(raw)
            if lvl is not None:
                return {"kind": "level", "name": "load", "value": lvl[0],
                        "level": lvl[1], "unit": None, "raw": str(raw)}
            m = _NUM_UNIT_RE.match(raw.replace(",", ""))
            if m:
                num = float(m.group(1))
                unit = (m.group(2) or "").strip() or None
                return {"kind": "number", "name": "load", "value": num,
                        "unit": unit, "raw": str(raw)}
        if isinstance(raw, (int, float)):
            return {"kind": "number", "name": "load", "value": float(raw),
                    "unit": None, "raw": str(raw)}

    if isinstance(raw, bool):
        return {"kind": "text", "name": canon, "value": str(raw), "unit": None, "raw": str(raw)}
    if isinstance(raw, (int, float)):
        default_unit = "mm" if str(name).endswith("_mm") else None
        return {"kind": "number", "name": canon, "value": float(raw),
                "unit": default_unit, "raw": str(raw)}
    text = str(raw).strip().replace(",", "")
    m = _NUM_UNIT_RE.match(text)
    if m:
        num = float(m.group(1))
        unit = (m.group(2) or "").strip() or None
        if unit is None and name.endswith("_mm"):
            unit = "mm"
        return {"kind": "number", "name": canon, "value": num,
                "unit": unit, "raw": str(raw)}
    return {"kind": "text", "name": canon, "value": text, "unit": None, "raw": str(raw)}


def _to_base(attr: dict[str, Any]) -> tuple[float, str] | None:
    """把数值属性换算到基准单位，返回 (value, dimension)。"""
    if attr.get("kind") != "number":
        return None
    unit = (attr.get("unit") or "").lower()
    value = float(attr["value"])
    if not unit:
        return value, "dimensionless"
    if unit in _UNIT_FACTORS:
        dim, factor = _UNIT_FACTORS[unit]
        return value * factor, dim
    return value, f"unit:{unit}"


def compare_attribute(name: str, a: Any, b: Any, *, rel_tol: float = 1e-6,
                      abs_tol: float = 1e-6) -> dict[str, Any]:
    """比较两个规范化（或原始）属性，返回状态与证据。

    status: match / conflict / missing_both / missing_one / incomparable
    """
    an = a if isinstance(a, dict) and "kind" in a else normalize_attribute(name, a)
    bn = b if isinstance(b, dict) and "kind" in b else normalize_attribute(name, b)
    if an is None and bn is None:
        return {"name": name, "status": "missing_both", "a": None, "b": None}
    if an is None or bn is None:
        return {"name": name, "status": "missing_one",
                "a": an.get("raw") if an else None, "b": bn.get("raw") if bn else None}

    status = "incomparable"
    if an["kind"] == "level" and bn["kind"] == "level":
        status = "match" if an.get("level") == bn.get("level") else "conflict"
    elif an["kind"] == "number" and bn["kind"] == "number":
        ba, bb = _to_base(an), _to_base(bn)
        if ba and bb and ba[1] == bb[1]:
            diff = abs(ba[0] - bb[0])
            tol = max(abs_tol, rel_tol * max(abs(ba[0]), abs(bb[0])))
            status = "match" if diff <= tol else "conflict"
    elif an["kind"] == "text" and bn["kind"] == "text":
        status = "match" if str(an["value"]).strip().lower() == str(bn["value"]).strip().lower() else "conflict"

    return {
        "name": name, "status": status,
        "a": an.get("raw", an.get("value")), "b": bn.get("raw", bn.get("value")),
        "a_normalized": an, "b_normalized": bn,
    }


def build_spec(*, model: str | None = None, manufacturer: str | None = None,
               name: str | None = None, load: Any = None,
               dimensions: dict[str, Any] | str | None = None,
               params: dict[str, Any] | None = None,
               category: str | None = None, **extra: Any) -> dict[str, Any]:
    """根据已知键构造规范化 Spec。未知额外键放入 params。"""
    dimensions_text = None
    if isinstance(dimensions, str):
        dimensions_text = dimensions
        dimensions = None
    dims: dict[str, Any] = {}
    prms: dict[str, Any] = {}

    if load is not None and load != "":
        attr = normalize_attribute("load", load)
        if attr:
            prms["load"] = attr

    for key, value in (dimensions or {}).items():
        canon = FIELD_ALIASES.get(str(key).strip().lower())
        if canon and canon[0] == "dim":
            attr = normalize_attribute(canon[1], value)
            if attr:
                dims[canon[1]] = attr
        else:
            attr = normalize_attribute(str(key), value)
            if attr:
                dims[str(key)] = attr

    for key, value in (params or {}).items():
        canon = FIELD_ALIASES.get(str(key).strip().lower())
        target = canon[1] if canon and canon[0] == "param" else str(key)
        attr = normalize_attribute(target, value)
        if attr:
            prms[target] = attr

    # 支持 "50x90x20" 形式的整体尺寸串
    for key in ("dimensions_text", "size", "dim_text"):
        text = extra.pop(key, None)
        if isinstance(text, str) and dimensions_text is None:
            dimensions_text = text
    if dimensions_text:
        m = _DIM_TRIPLE_RE.match(dimensions_text)
        if m:
            for dim_name, num in zip(("bore_mm", "outer_diameter_mm", "width_mm"), m.groups()):
                dims.setdefault(dim_name, normalize_attribute(dim_name, f"{num} mm"))

    _IGNORE_EXTRA = {"equipment_model", "device_model", "serial", "asset", "asset_id",
                     "equipment", "note", "description"}
    for key, value in extra.items():
        kl = str(key).strip().lower()
        if kl in _IGNORE_EXTRA:
            continue
        canon = FIELD_ALIASES.get(kl)
        if canon:
            kind, target = canon
            bucket = dims if kind == "dim" else prms
            attr = normalize_attribute(target, value)
            if attr:
                bucket[target] = attr
        elif kl in ("bore_mm", "outer_diameter_mm", "width_mm", "length_mm"):
            attr = normalize_attribute(kl, value)
            if attr:
                dims[kl] = attr
        else:
            # 未知名义参数也保留为可比较的文本/数值属性，避免丢失证据
            attr = normalize_attribute(kl, value)
            if attr:
                prms[kl] = attr

    return {
        "category": category or infer_category(model, name),
        "manufacturer": (str(manufacturer).strip() if manufacturer not in (None, "") else None),
        "model": (str(model).strip() if model not in (None, "") else None),
        "model_key": model_key(model),
        "name": name,
        "dimensions": dims,
        "params": prms,
    }


def merge_spec(existing: dict[str, Any] | None, incoming: dict[str, Any] | None
               ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """合并两次来源观察得到的 Spec。

    * incoming 未提供的属性保留 existing —— 稀疏报价行不会抹掉目录里的额定参数；
    * incoming 显式给出且与 existing 冲突的值，以新观察为准并记录 change，
      旧值仍可在 raw versions 中追溯。
    返回 (merged_spec, changes)。
    """
    if not existing:
        return incoming or build_spec(), []
    if not incoming:
        return existing, []
    merged = dict(existing)
    changes: list[dict[str, Any]] = []
    for key in ("manufacturer", "category", "name"):
        if incoming.get(key):
            merged[key] = incoming[key]
    if incoming.get("model"):
        merged["model"] = incoming["model"]
        merged["model_key"] = incoming.get("model_key") or model_key(incoming["model"])

    for bucket in ("dimensions", "params"):
        mb = dict(existing.get(bucket, {}))
        for name, attr in (incoming.get(bucket, {}) or {}).items():
            old = mb.get(name)
            if old is not None:
                cmp_ = compare_attribute(name, old, attr)
                if cmp_["status"] == "conflict":
                    changes.append({"attribute": name, "from": old.get("raw", old.get("value")),
                                    "to": attr.get("raw", attr.get("value"))})
            mb[name] = attr
        merged[bucket] = mb
    return merged, changes


def all_attributes(spec: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """返回 attr_name -> ('dim'|'param', attr)。"""
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, attr in (spec or {}).get("dimensions", {}).items():
        out[name] = ("dim", attr)
    for name, attr in (spec or {}).get("params", {}).items():
        out[name] = ("param", attr)
    return out


def evaluate_identity(a: dict[str, Any], b: dict[str, Any], *,
                      required_attributes: Iterable[str] | None = None,
                      similarity_threshold: float = 0.75) -> dict[str, Any]:
    """保守评估两个 Spec 是否可自动认定同一物料。

    返回 dict：``status``(confirmed/pending)、``match_kind``、``reasons``、
    ``comparisons``、``similarity``、``matching_attributes``。
    """
    from .status import (CONFIRMED, PENDING, reason_text)  # 局部导入避免循环

    reasons: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []

    rel = model_relation(a.get("model"), b.get("model"))
    sim = max(name_similarity(a.get("model"), b.get("model")),
              name_similarity(a.get("name"), b.get("name")))

    if rel == "case_only":
        reasons.append({"code": "model_case_mismatch",
                        "message": reason_text("model_case_mismatch")})
    elif rel == "similar":
        reasons.append({"code": "similarity_not_evidence",
                        "message": reason_text("similarity_not_evidence"),
                        "similarity": sim})
    elif rel == "different":
        reasons.append({"code": "manufacturer_code_mismatch",
                        "message": reason_text("manufacturer_code_mismatch")})
    elif rel == "missing":
        reasons.append({"code": "unknown_manufacturer_model",
                        "message": reason_text("unknown_manufacturer_model")})

    attrs_a = all_attributes(a)
    attrs_b = all_attributes(b)
    names = sorted(set(attrs_a) | set(attrs_b))

    matching = 0
    conflict = False
    for name in names:
        in_a = attrs_a.get(name)
        in_b = attrs_b.get(name)
        kind = (in_a or in_b)[0]
        raw_a = in_a[1] if in_a else None
        raw_b = in_b[1] if in_b else None
        cmp_ = compare_attribute(name, raw_a, raw_b)
        cmp_["kind"] = kind
        comparisons.append(cmp_)
        st = cmp_["status"]
        if st == "match":
            matching += 1
        elif st == "conflict":
            conflict = True
            code = "dimension_conflict" if kind == "dim" else "parameter_conflict"
            reasons.append({"code": code, "attribute": name,
                            "message": reason_text(
                                code, name=name,
                                value=cmp_.get("a"), other=cmp_.get("b"))})
        elif st == "missing_one":
            code = "missing_dimension" if kind == "dim" else "missing_attribute"
            present = cmp_.get("a") if cmp_.get("a") not in (None, "") else cmp_.get("b")
            reasons.append({"code": code, "attribute": name,
                            "message": reason_text(code, name=name, kind="尺寸" if kind == "dim" else "参数")})
        elif st == "incomparable":
            reasons.append({"code": "incomparable_attribute", "attribute": name,
                            "message": reason_text("incomparable_attribute", name=name)})

    # 策略声明“必须存在”的属性（即便双方都没有也要提示信息不足）
    for req in required_attributes or ():
        if req not in attrs_a and req not in attrs_b:
            reasons.append({"code": "missing_parameter", "attribute": req,
                            "message": reason_text("missing_parameter", name=req)})

    reliable_code = rel in ("exact", "normalized_code")
    has_blocking = any(r["code"] in (
        "parameter_conflict", "dimension_conflict", "model_case_mismatch",
        "similarity_not_evidence", "manufacturer_code_mismatch",
        "unknown_manufacturer_model", "incomparable_attribute",
    ) or r["code"].startswith("missing") for r in reasons)

    # 严格策略：至少一项硬属性相互匹配，否则证据不足。
    if reliable_code and matching == 0 and not conflict:
        reasons.append({"code": "insufficient_identity_evidence",
                        "message": reason_text("insufficient_identity_evidence")})

    confirmed = reliable_code and not has_blocking and matching >= 1
    status = CONFIRMED if confirmed else PENDING

    return {
        "status": status,
        "match_kind": rel,
        "similarity": sim,
        "reasons": reasons,
        "comparisons": comparisons,
        "matching_attributes": matching,
        "has_conflict": conflict,
    }


# ---------------------------------------------------------------------------
# 替代审批的参数范围覆盖
# ---------------------------------------------------------------------------

def _level_value(spec_attr: dict[str, Any], range_token: Any) -> int | None:
    if isinstance(range_token, str):
        lvl = _canonical_load_level(range_token)
        if lvl:
            return lvl[1]
    return normalize_attribute("load", range_token).get("level") if range_token is not None else None


def value_covers(attr_name: str, required_value: Any, range_spec: Any) -> dict[str, Any]:
    """判断需求属性值是否落在审批参数范围内。

    支持的 range_spec：
      * 标量（精确相等，或等级下限简写）
      * ``{"min": x, "max": y}``（任一端可省）
      * ``[lo, hi]``
      * ``{"values": [..]}`` / ``{"in": [..]}`` 允许的离散值
    """
    req = required_value if isinstance(required_value, dict) and "kind" in required_value \
        else normalize_attribute(attr_name, required_value)
    if req is None:
        return {"covered": False, "reason": "required_value_missing"}
    if range_spec is None:
        return {"covered": False, "reason": "range_missing"}

    allowed = None
    lo = hi = None
    if isinstance(range_spec, dict):
        if "values" in range_spec or "in" in range_spec:
            allowed = range_spec.get("values", range_spec.get("in"))
        lo = range_spec.get("min", range_spec.get("from"))
        hi = range_spec.get("max", range_spec.get("to"))
    elif isinstance(range_spec, (list, tuple)):
        if len(range_spec) == 2:
            lo, hi = range_spec
        else:
            allowed = list(range_spec)
    else:
        # 标量：数值视为“下限”，离散值视为允许值
        if req.get("kind") == "number":
            lo = range_spec
        else:
            allowed = [range_spec]

    if allowed is not None:
        wanted = str(req.get("value") if req.get("kind") != "level" else req.get("value")).lower()
        ok = wanted in {str(v).lower() for v in allowed}
        return {"covered": ok, "reason": None if ok else "not_in_allowed_values",
                "range": allowed}

    if req.get("kind") == "level":
        req_level = int(req.get("level", 0))
        if lo is not None:
            lv = _level_value(req, lo)
            lo_num = lv if lv is not None else int(lo)
            if req_level < lo_num:
                return {"covered": False, "reason": "below_min", "range": range_spec}
        if hi is not None:
            lv = _level_value(req, hi)
            hi_num = lv if lv is not None else int(hi)
            if req_level > hi_num:
                return {"covered": False, "reason": "above_max", "range": range_spec}
        return {"covered": True, "range": range_spec}

    if req.get("kind") == "number":
        base = _to_base(req)
        req_num = base[0] if base else float(req["value"])
        req_dim = base[1] if base else "dimensionless"

        def _bound(token: Any) -> float | None:
            b = normalize_attribute(attr_name, token)
            if b is None:
                return parse_float(token)
            bb = _to_base(b)
            if bb and bb[1] == req_dim:
                return bb[0]
            return float(b["value"])

        if lo is not None:
            lo_num = _bound(lo)
            if lo_num is not None and req_num + 1e-9 < lo_num:
                return {"covered": False, "reason": "below_min", "range": range_spec}
        if hi is not None:
            hi_num = _bound(hi)
            if hi_num is not None and req_num - 1e-9 > hi_num:
                return {"covered": False, "reason": "above_max", "range": range_spec}
        return {"covered": True, "range": range_spec}

    # 文本：相等或通配
    if lo is None and hi is None:
        return {"covered": str(req.get("value")) == str(range_spec), "range": range_spec}
    return {"covered": False, "reason": "unsupported_range", "range": range_spec}


def parse_float(token: Any) -> float | None:
    try:
        return float(token)
    except (TypeError, ValueError):
        return None
