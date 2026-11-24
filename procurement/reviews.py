"""复核事项的生成与对账（reconcile）。

系统每次采集、更正映射、撤回公告或重算后，调用 :func:`sync_review_items`：

* 当前仍成立的问题 -> 对应复核事项保持 open（幂等，不重复轰炸）；
* 新出现的问题（例如新公告导致交付日期被覆盖）-> 新建带证据的复核事项；
* 因更正/撤回而消失的问题 -> 自动把对应 open 事项标记 resolved 并留痕；
* 人工事项（``auto=False``）不被自动对账改动。
"""
from __future__ import annotations

from typing import Any

from .common import deterministic_id, iso, utc_now
from .decisions import (check_order, ensure_imported_baseline,
                        scan_confirmed_order_risks)
from .status import (ORDER_OPEN_STATES, ORDER_PLACED_STATES, REVIEW_OPEN,
                     REVIEW_RESOLVED, SEV_BLOCKER, SEV_WARNING)


def _rid(*parts: Any) -> str:
    return deterministic_id("REV", *parts, length=12)


def collect_desired_findings(store, *, as_of_date=None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # ---- 物料 / 经销商编码待核 -------------------------------------------
    for lkey, listing in store.listings.items():
        mapping = listing.get("mapping") or {}
        status = mapping.get("status")
        if status not in ("pending", "rejected"):
            continue
        reasons = mapping.get("reasons") or [{"code": "insufficient_identity_evidence"}]
        for reason in reasons:
            code = reason.get("code", "pending_verification")
            findings.append({
                "dedup": ("material", listing.get("material_id"), lkey, code,
                          reason.get("attribute")),
                "subject_type": "material",
                "subject_id": listing.get("material_id") or lkey,
                "material_id": listing.get("material_id"),
                "listing_key": lkey,
                "order_id": None,
                "kind": "material_verification",
                "code": code,
                "severity": SEV_WARNING if status == "pending" else SEV_BLOCKER,
                "blocks_release": True,
                "title": "物料映射待核" if status == "pending" else "物料映射已排除",
                "message": reason.get("message", code),
                "suggested_action": reason.get("suggested_action"),
                "attribute": reason.get("attribute"),
                "evidence": [{
                    "kind": "source_record", "source_id": listing.get("source_id"),
                    "item": listing.get("item"), "model": listing.get("model"),
                    **((listing.get("catalog") or {}).get("provenance") or {})}],
            })

    # ---- 订单 -------------------------------------------------------------
    ensure_imported_baseline(store)
    for oid, order in store.orders.items():
        if order.get("state") in ORDER_PLACED_STATES or order.get("snapshot"):
            risk_scan = scan_confirmed_order_risks(store, oid, as_of_date=as_of_date)
            for risk in risk_scan["risks"]:
                findings.append({
                    "dedup": ("order-risk", oid, risk.get("code"),
                              risk.get("announcement_id"), risk.get("approval_id"),
                              risk.get("lkey")),
                    "subject_type": "order", "subject_id": oid, "order_id": oid,
                    "material_id": None,
                    "kind": "confirmed_order_risk",
                    "code": risk["code"],
                    "severity": risk.get("severity", SEV_WARNING),
                    "blocks_release": False,
                    "new_announcement": risk.get("new_announcement", False),
                    "title": "已确认订单风险 / 复核",
                    "message": risk["message"],
                    "suggested_action": risk["suggested_action"],
                    "evidence": risk.get("evidence", []),
                    "dates": risk.get("dates"),
                })
        else:
            check = order.get("last_check")
            if check is None or as_of_date is not None:
                check = check_order(store, oid, as_of_date=as_of_date)
            for f in check.get("blockers", []):
                findings.append(_order_finding(oid, f, SEV_BLOCKER, blocks=True))
            for f in check.get("holds", []):
                findings.append(_order_finding(oid, f, SEV_WARNING, blocks=True))
    return findings


def _order_finding(oid: str, f: dict[str, Any], severity: str, *, blocks: bool) -> dict[str, Any]:
    return {
        "dedup": ("order", oid, f.get("code"), f.get("attribute"), f.get("approval_id")),
        "subject_type": "order", "subject_id": oid, "order_id": oid,
        "material_id": None, "kind": "pre_purchase_check",
        "code": f.get("code"), "severity": severity, "blocks_release": blocks,
        "title": "采购前核对未通过", "message": f.get("message", f.get("code")),
        "suggested_action": f.get("suggested_action"),
        "evidence": f.get("evidence", []),
    }


def sync_review_items(store, *, as_of_date=None, now=None) -> dict[str, Any]:
    ts = iso(now or utc_now())
    desired = collect_desired_findings(store, as_of_date=as_of_date)
    desired_by_id: dict[str, dict[str, Any]] = {}
    for f in desired:
        rid = _rid(*f["dedup"])
        f["review_id"] = rid
        desired_by_id[rid] = f

    created: list[str] = []
    reopened: list[str] = []
    for rid, f in desired_by_id.items():
        item = store.review_items.get(rid)
        if item is None:
            store.review_items[rid] = {
                "id": rid, "status": REVIEW_OPEN, "auto": True,
                "created_at": ts, "updated_at": ts,
                "resolved_at": None, "resolution_note": None,
                "seen_count": 1, **{k: v for k, v in f.items() if k != "dedup"},
            }
            created.append(rid)
        else:
            item["updated_at"] = ts
            item["seen_count"] = int(item.get("seen_count", 1)) + 1
            # 用最新证据/文案刷新，但保留首次创建时间
            item["message"] = f["message"]
            item["evidence"] = f.get("evidence", item.get("evidence", []))
            if item.get("status") == REVIEW_RESOLVED and item.get("auto", True):
                item["status"] = REVIEW_OPEN
                item["resolved_at"] = None
                item["resolution_note"] = None
                reopened.append(rid)

    resolved: list[str] = []
    for rid, item in list(store.review_items.items()):
        if rid in desired_by_id:
            continue
        if not item.get("auto", True):
            continue
        if item.get("status") == REVIEW_OPEN:
            item["status"] = REVIEW_RESOLVED
            item["resolved_at"] = ts
            item["resolution_note"] = "触发条件已消失（物料映射更正、公告撤回或重新计算），系统自动解决；记录保留可追溯。"
            resolved.append(rid)

    open_items = [r for r in store.review_items.values() if r.get("status") == REVIEW_OPEN]
    return {"created": created, "reopened": reopened, "resolved": resolved,
            "open_count": len(open_items),
            "new_delivery_reviews": [
                rid for rid in created
                if store.review_items[rid].get("code") == "delivery_date_superseded"]}


def open_reviews(store, *, subject_type: str | None = None,
                 order_id: str | None = None, material_id: str | None = None,
                 code: str | None = None) -> list[dict[str, Any]]:
    out = []
    for r in store.review_items.values():
        if r.get("status") != REVIEW_OPEN:
            continue
        if subject_type and r.get("subject_type") != subject_type:
            continue
        if order_id and r.get("order_id") != order_id:
            continue
        if material_id and r.get("material_id") != material_id:
            continue
        if code and r.get("code") != code:
            continue
        out.append(r)
    return sorted(out, key=lambda r: (0 if r.get("severity") == SEV_BLOCKER else 1,
                                      r.get("created_at") or ""))
