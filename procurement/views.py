"""采购员视图：按物料、按订单汇总来源差异、待核原因、证据与后续处置。"""
from __future__ import annotations

from typing import Any

from .announcements import effective_announcements
from .decisions import (check_order, commercial_position,
                        scan_confirmed_order_risks)
from .governance import resolve_material_identifier
from .reviews import open_reviews
from .specs import all_attributes
from .status import ORDER_PLACED_STATES, STATUS_LABELS, suggested_action


def _attr_raw_map(spec: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, (_kind, attr) in all_attributes(spec or {}).items():
        out[name] = attr.get("raw", attr.get("value"))
    return out


def _listings_for_material(store, mat: dict[str, Any]) -> list[dict[str, Any]]:
    keys = {mat.get("model_key")}
    for code in mat.get("codes", []):
        from .specs import model_key
        keys.add(model_key(code))
    out = []
    for ln in store.listings.values():
        if ln.get("material_id") == mat["id"]:
            out.append(ln)
        elif ln.get("spec", {}).get("model_key") in keys:
            out.append(ln)
    # 去重
    uniq = {ln["key"]: ln for ln in out}
    return list(uniq.values())


def material_view(store, identifier: str) -> dict[str, Any]:
    mat = resolve_material_identifier(store, identifier)
    if mat is None:
        raise KeyError(f"无法定位物料: {identifier}")
    listings = _listings_for_material(store, mat)

    source_rows = []
    for ln in listings:
        comp = commercial_position(ln)
        prov = (ln.get("catalog") or {}).get("provenance") or {}
        source_rows.append({
            "listing_key": ln["key"], "source_id": ln.get("source_id"),
            "item": ln.get("item"), "name": ln.get("name"),
            "model": ln.get("model"),
            "attributes": _attr_raw_map(ln.get("spec")),
            "available": comp.get("available"), "price": comp.get("price"),
            "currency": comp.get("currency"), "lead_days": comp.get("lead_days"),
            "mapping_status": (ln.get("mapping") or {}).get("status"),
            "mapping_status_label": STATUS_LABELS.get((ln.get("mapping") or {}).get("status")),
            "reference": (ln.get("mapping") or {}).get("reference"),
            "source_updated_at": prov.get("source_updated_at"),
            "fetched_at": prov.get("fetched_at"),
            "version_id": prov.get("version_id"),
            "payload_hash": prov.get("payload_hash"),
        })

    differences = _attribute_differences(listings)
    offers = _offer_comparison(listings)

    anns_all = [a for a in store.announcements.values()
                if a.get("model_key") == mat.get("model_key")
                or a.get("replacement_model_key") == mat.get("model_key")]
    approvals = [ap for ap in store.approvals.values()
                 if ap.get("from_material_id") == mat["id"]
                 or ap.get("to_material_id") == mat["id"]
                 or ap.get("from_model_key") == mat.get("model_key")
                 or ap.get("to_model_key") == mat.get("model_key")]

    order_refs = _orders_for_listings(store, listings)
    reviews = open_reviews(store, material_id=mat["id"])

    pending_reasons, next_actions = _pending_summary(listings, reviews)

    return {
        "view": "material",
        "material": {
            "id": mat["id"], "canonical_model": mat.get("canonical_model"),
            "model_key": mat.get("model_key"), "manufacturer": mat.get("manufacturer"),
            "category": mat.get("category"),
            "governance_status": mat.get("governance_status"),
            "governance_status_label": STATUS_LABELS.get(mat.get("governance_status")),
            "codes": mat.get("codes"), "spec": mat.get("spec"),
        },
        "source_count": len(listings),
        "sources": source_rows,
        "source_differences": differences,
        "offers": offers,
        "announcements": sorted(
            [{"id": a["id"], "kind": a.get("kind"), "status": a.get("status"),
              "effective_date": a.get("effective_date"), "title": a.get("title"),
              "replacement_model": a.get("replacement_model"),
              "lead_days": a.get("lead_days"),
              "earliest_delivery_date": a.get("earliest_delivery_date"),
              "provenance": a.get("provenance")} for a in anns_all],
            key=lambda a: a.get("effective_date") or ""),
        "approvals": approvals,
        "related_orders": order_refs,
        "pending_reasons": pending_reasons,
        "next_actions": next_actions,
        "review_items": reviews,
    }


def _attribute_differences(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observed: dict[str, list[dict[str, Any]]] = {}
    for ln in listings:
        for name, raw in _attr_raw_map(ln.get("spec")).items():
            observed.setdefault(name, []).append(
                {"source_id": ln.get("source_id"), "item": ln.get("item"), "value": raw})
        comp = commercial_position(ln)
        for name, value in (("available", comp.get("available")),
                            ("price", comp.get("price")),
                            ("lead_days", comp.get("lead_days"))):
            if value is not None:
                observed.setdefault(name, []).append(
                    {"source_id": ln.get("source_id"), "item": ln.get("item"), "value": value})
    diffs = []
    for name, values in observed.items():
        distinct = {str(v["value"]) for v in values}
        if len(distinct) > 1:
            conflict = name in ("load",) or name.endswith("_mm")
            diffs.append({"attribute": name, "distinct_values": len(distinct),
                          "conflict": conflict, "values": values})
    return sorted(diffs, key=lambda d: (not d["conflict"], d["attribute"]))


def _offer_comparison(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offers = []
    for ln in listings:
        comp = commercial_position(ln)
        offers.append({"source_id": ln.get("source_id"), "item": ln.get("item"),
                       "available": comp.get("available"), "price": comp.get("price"),
                       "currency": comp.get("currency"),
                       "lead_days": comp.get("lead_days"),
                       "mapping_status": (ln.get("mapping") or {}).get("status"),
                       "terms_count": len(comp.get("terms_history", []))})
    return sorted(offers, key=lambda o: (-(o.get("available") or -1), o.get("price") or 1e12))


def _orders_for_listings(store, listings) -> list[dict[str, Any]]:
    items = {ln.get("item") for ln in listings}
    lkeys = {ln.get("key") for ln in listings}
    out = []
    for oid, o in store.orders.items():
        if o.get("item") in items:
            out.append({"order_id": oid, "state": o.get("state"), "item": o.get("item"),
                        "due": o.get("due"), "asset": o.get("asset")})
        elif o.get("source") and f"{o['source']}::{o['item']}" in lkeys:
            out.append({"order_id": oid, "state": o.get("state"), "item": o.get("item"),
                        "due": o.get("due"), "asset": o.get("asset")})
    return out


def _pending_summary(listings, reviews) -> tuple[list[dict[str, Any]], list[str]]:
    reasons: list[dict[str, Any]] = []
    actions: list[str] = []
    for ln in listings:
        m = ln.get("mapping") or {}
        if m.get("status") == "confirmed":
            continue
        for r in m.get("reasons", []):
            reasons.append({"listing_key": ln["key"], "source_id": ln.get("source_id"),
                            "item": ln.get("item"), **r})
            act = suggested_action(r.get("code", ""))
            if act and act not in actions:
                actions.append(act)
    for rv in reviews:
        if rv.get("status") == "open" and rv.get("suggested_action") not in actions:
            actions.append(rv["suggested_action"])
    if not reasons and reviews:
        reasons = [{"code": r.get("code"), "message": r.get("message")} for r in reviews]
    return reasons, actions


def order_view(store, order_id: str, *, as_of_date=None) -> dict[str, Any]:
    if order_id not in store.orders:
        raise KeyError(order_id)
    order = store.orders[order_id]
    placed = order.get("state") in ORDER_PLACED_STATES or order.get("snapshot")

    reviews = open_reviews(store, order_id=order_id)
    view: dict[str, Any] = {
        "view": "order",
        "order": {k: order.get(k) for k in
                   ("order_id", "asset", "item", "source", "due", "qty", "state")},
        "placed": bool(placed),
        "review_items": reviews,
        "timeline": [e for e in store.data.get("audit", [])
                     if e.get("details", {}).get("order") == order_id
                     or e.get("details", {}).get("listing", "").endswith(str(order.get("item")))],
    }

    if placed:
        risk_scan = scan_confirmed_order_risks(store, order_id, as_of_date=as_of_date)
        view["original_decision"] = order.get("original_decision")
        view["snapshot"] = order.get("snapshot")
        view["snapshot_immutable"] = True
        view["current_risks"] = risk_scan["risks"]
        view["current_assessment"] = risk_scan.get("check") and {
            "as_of": risk_scan["check"].get("as_of"),
            "verdict": risk_scan["check"].get("verdict"),
            "blockers": risk_scan["check"].get("blockers"),
            "holds": risk_scan["check"].get("holds"),
        }
        view["check"] = risk_scan["check"]
    else:
        check = check_order(store, order_id, as_of_date=as_of_date)
        order["last_check"] = check
        view["check"] = check
        view["verdict"] = check["verdict"]
        view["can_release"] = check["can_release"]

    # 关联物料视图（来源差异）
    mat_view = None
    try:
        from .decisions import find_item_listings
        lns = find_item_listings(store, order.get("item"), order.get("source"))
        if lns:
            mid = lns[0].get("material_id")
            if mid:
                mat_view = material_view(store, mid)
    except KeyError:
        mat_view = None
    view["material_overview"] = _compact_material(mat_view) if mat_view else None
    return view


def _compact_material(mv: dict[str, Any] | None) -> dict[str, Any] | None:
    if not mv:
        return None
    return {"material": mv["material"], "sources": mv["sources"],
            "source_differences": mv["source_differences"],
            "pending_reasons": mv["pending_reasons"],
            "next_actions": mv["next_actions"]}


def dashboard(store) -> dict[str, Any]:
    reviews = open_reviews(store)
    pending_materials = [m for m in store.materials.values()
                         if m.get("governance_status") == "pending"]
    draft_orders = [o for o in store.orders.values() if o.get("state") == "draft"]
    confirmed_orders = [o for o in store.orders.values()
                        if o.get("state") in ORDER_PLACED_STATES]
    releasable = blocked = pending = 0
    for o in draft_orders:
        chk = o.get("last_check")
        if not chk:
            continue
        if chk.get("can_release"):
            releasable += 1
        elif chk.get("verdict") == "blocked":
            blocked += 1
        else:
            pending += 1
    return {
        "view": "dashboard",
        "counts": {
            "sources": len(store.sources),
            "listings": len(store.listings),
            "materials": len(store.materials),
            "pending_materials": len(pending_materials),
            "draft_orders": len(draft_orders),
            "confirmed_orders": len(confirmed_orders),
            "draft_releasable": releasable,
            "draft_blocked": blocked,
            "draft_pending": pending,
            "open_review_items": len(reviews),
            "blocker_reviews": sum(1 for r in reviews if r.get("severity") == "blocker"),
            "announcements_active": sum(1 for a in store.announcements.values()
                                        if a.get("status") == "active"),
            "approvals": len(store.approvals),
        },
        "review_items": reviews,
        "pending_material_ids": [m["id"] for m in pending_materials],
        "source_checkpoints": list(store.checkpoints.values()),
    }
