"""厂商公告：版本化入库、当前生效视图、撤回。

关键纪律：公告与报价是不同类型的事实。再旧的报价也不能覆盖/抹掉一条
已生效的停产公告；决策时分别读取并按“截至 as_of 仍生效”的公告核对。
"""
from __future__ import annotations

from typing import Any

from .common import iso, parse_date, parse_datetime, short_hash, utc_now
from .specs import model_key
from .status import ANN_ACTIVE, ANN_WITHDRAWN


def upsert_announcement(store, adapted: dict[str, Any], *, provenance: dict[str, Any],
                        external_id: str | None = None) -> tuple[dict[str, Any], bool]:
    ext = external_id or adapted.get("announcement_id")
    model = adapted.get("model")
    # 没有外部编号时，用来源+型号+类型+发布时间构造稳定编号
    ann_id = ext or "ANN-" + short_hash(
        provenance.get("source_id"), model, adapted.get("kind"),
        adapted.get("published_at_raw"), length=10)

    existing = store.announcements.get(ann_id)
    eff_dt = parse_datetime(adapted.get("effective_at_raw"))
    pub_dt = parse_datetime(adapted.get("published_at_raw"))
    record = existing or {
        "id": ann_id,
        "status": ANN_ACTIVE,
        "withdrawn_at": None,
        "withdraw_reason": None,
        "created_at": iso(utc_now()),
        "revision": 0,
    }
    record.update({
        "source_id": provenance.get("source_id"),
        "external_id": ext,
        "manufacturer": adapted.get("manufacturer"),
        "model": model,
        "model_key": model_key(model),
        "kind": adapted.get("kind"),
        "title": adapted.get("title"),
        "body": adapted.get("body"),
        "url": adapted.get("url"),
        "published_at": iso(pub_dt),
        "published_date": (parse_date(adapted.get("published_at_raw")).isoformat()
                           if parse_date(adapted.get("published_at_raw")) else None),
        "effective_at": iso(eff_dt),
        "effective_date": (parse_date(adapted.get("effective_at_raw")).isoformat()
                           if parse_date(adapted.get("effective_at_raw")) else None),
        "last_order_date": (parse_date(adapted.get("last_order_date_raw")).isoformat()
                            if adapted.get("last_order_date_raw") else None),
        "replacement_model": adapted.get("replacement_model"),
        "replacement_model_key": model_key(adapted.get("replacement_model")),
        "lead_days": adapted.get("lead_days"),
        "earliest_delivery_date": (
            parse_date(adapted.get("earliest_delivery_date_raw")).isoformat()
            if adapted.get("earliest_delivery_date_raw") else None),
        "equipment_models_hint": adapted.get("equipment_models_hint", []),
        "parameter_ranges_hint": adapted.get("parameter_ranges_hint"),
        "provenance": provenance,
        "raw_version_id": provenance.get("version_id"),
        "updated_at": iso(utc_now()),
    })
    record["revision"] = int(record.get("revision", 0)) + (1 if existing else 0)
    store.announcements[ann_id] = record

    # 为公告型号/替代型号确保物料占位
    from .governance import ensure_material
    if model:
        ensure_material(store, model=model, manufacturer=adapted.get("manufacturer"))
    if adapted.get("replacement_model"):
        ensure_material(store, model=adapted["replacement_model"],
                        manufacturer=adapted.get("manufacturer"))
    return record, existing is None


def withdraw_announcement(store, ann_id: str, *, reason: str = "", by: str = "governance") -> dict[str, Any]:
    if ann_id not in store.announcements:
        raise KeyError(f"未知公告: {ann_id}")
    ann = store.announcements[ann_id]
    ann["status"] = ANN_WITHDRAWN
    ann["withdrawn_at"] = iso(utc_now())
    ann["withdraw_reason"] = reason
    ann["withdrawn_by"] = by
    store.audit("announcement.withdraw", actor=by, announcement=ann_id, reason=reason)
    return ann


def is_effective(ann: dict[str, Any], as_of_date) -> bool:
    if ann.get("status") == ANN_WITHDRAWN:
        return False
    eff = parse_date(ann.get("effective_date")) or parse_date(ann.get("published_date"))
    if eff is None:
        return True
    return eff <= as_of_date


def effective_announcements(store, *, model: str | None = None,
                           model_key_value: str | None = None,
                           material_id: str | None = None,
                           as_of_date=None) -> list[dict[str, Any]]:
    """返回截至 as_of_date 已生效且未撤回的公告。"""
    if as_of_date is None:
        as_of_date = utc_now().date()
    key = model_key_value or model_key(model)
    mid_keys: set[str] = set()
    if material_id and material_id in store.materials:
        mat = store.materials[material_id]
        if mat.get("model_key"):
            mid_keys.add(mat["model_key"])
        for code in mat.get("codes", []):
            if model_key(code):
                mid_keys.add(model_key(code))
    if key:
        mid_keys.add(key)

    out = []
    for ann in store.announcements.values():
        if not is_effective(ann, as_of_date):
            continue
        if not mid_keys:
            out.append(ann)
            continue
        if ann.get("model_key") in mid_keys:
            out.append(ann)
        elif ann.get("replacement_model_key") in mid_keys:
            out.append(ann)
    return out


def upcoming_announcements(store, *, model_key_value: str | None = None,
                           as_of_date=None, within_days: int | None = None) -> list[dict[str, Any]]:
    if as_of_date is None:
        as_of_date = utc_now().date()
    key = model_key_value
    out = []
    for ann in store.announcements.values():
        if ann.get("status") == ANN_WITHDRAWN:
            continue
        eff = parse_date(ann.get("effective_date"))
        if eff is None or eff <= as_of_date:
            continue
        if within_days is not None and (eff - as_of_date).days > within_days:
            continue
        if key is None or ann.get("model_key") == key:
            out.append(ann)
    return out
