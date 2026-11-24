"""采购决策核心：订单发出前核对供货、停产、交期与替代审批。

设计要点：

* 候选是否可放行，必须同时通过 映射/参数符合性、库存、当前生效停产、交期 四类核对；
* 替代件仅当存在有效审批，且 **设备型号范围** 与 **参数范围** 都覆盖时才可放行；
* 已确认订单保存不可变 ``snapshot``，普通目录刷新不改写其原决策，只刷新风险；
* 所有结论都带 ``evidence``（来源、原始版本哈希、抓取时间、公告/审批 ID）。
"""
from __future__ import annotations

from typing import Any

from .announcements import effective_announcements
from .common import add_days, iso, parse_date, utc_now
from .governance import approval_is_active
from .specs import evaluate_identity, value_covers
from .status import (BLOCKED, CONFIRMED_ORDER, DRAFT, ORDER_OPEN_STATES, PENDING,
                     RELEASABLE, reason_text, suggested_action)


# ---------------------------------------------------------------------------
# 证据构造
# ---------------------------------------------------------------------------

def listing_evidence(listing: dict[str, Any]) -> dict[str, Any]:
    prov = (listing.get("catalog") or {}).get("provenance") or {}
    return {
        "kind": "source_record",
        "source_id": listing.get("source_id"),
        "item": listing.get("item"),
        "model": listing.get("model"),
        "version_id": prov.get("version_id"),
        "payload_hash": prov.get("payload_hash"),
        "fetched_at": prov.get("fetched_at"),
        "source_updated_at": prov.get("source_updated_at"),
        "adapter": prov.get("adapter"),
        "url": prov.get("url"),
    }


def quote_evidence(quote: dict[str, Any]) -> dict[str, Any]:
    prov = quote.get("provenance") or {}
    return {"kind": "quote", "quote_id": quote.get("quote_id"),
            "source_id": quote.get("source_id"), "version_id": prov.get("version_id"),
            "payload_hash": prov.get("payload_hash"), "fetched_at": prov.get("fetched_at"),
            "quoted_at": quote.get("quoted_at")}


def announcement_evidence(ann: dict[str, Any]) -> dict[str, Any]:
    prov = ann.get("provenance") or {}
    return {"kind": "announcement", "announcement_id": ann.get("id"),
            "notice_kind": ann.get("kind"), "source_id": ann.get("source_id"),
            "version_id": ann.get("raw_version_id") or prov.get("version_id"),
            "payload_hash": prov.get("payload_hash"),
            "fetched_at": prov.get("fetched_at"),
            "effective_date": ann.get("effective_date"),
            "title": ann.get("title"), "url": ann.get("url")}


def _finding(code: str, severity: str = "blocker", **fmt: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity,
            "message": reason_text(code, **fmt),
            "suggested_action": suggested_action(code), **{k: v for k, v in fmt.items()
                                                           if k not in ("name", "value", "other")}}


# ---------------------------------------------------------------------------
# 候选定位与商业条件
# ---------------------------------------------------------------------------

def find_item_listings(store, item: str | None, source: str | None = None) -> list[dict[str, Any]]:
    if not item:
        return []
    if source:
        lk = f"{source}::{item}"
        return [store.listings[lk]] if lk in store.listings else []
    exact = [ln for ln in store.listings.values() if ln.get("item") == item]
    if exact:
        return exact
    # 兜底：订单里可能直接写厂商型号
    from .specs import model_key
    key = model_key(item)
    return [ln for ln in store.listings.values()
            if ln.get("spec", {}).get("model_key") == key]


def commercial_position(listing: dict[str, Any]) -> dict[str, Any]:
    """汇总目录行 + 各次报价中的库存/价格/交期，保留全部来源值。"""
    catalog = listing.get("catalog") or {}
    terms_history: list[dict[str, Any]] = []
    lead_values: list[int] = []
    price_values: list[float] = []
    avail = catalog.get("available")
    if catalog:
        terms_history.append({"via": "catalog", "available": catalog.get("available"),
                              "price": catalog.get("price"), "lead_days": catalog.get("lead_days"),
                              "provenance": catalog.get("provenance")})
        if catalog.get("lead_days") is not None:
            lead_values.append(int(catalog["lead_days"]))
        if catalog.get("price") is not None:
            price_values.append(float(catalog["price"]))

    latest_quote = None
    for q in sorted(listing.get("quotes", []), key=lambda q: q.get("quoted_at") or ""):
        t = q.get("terms") or {}
        terms_history.append({"via": f"quote:{q.get('quote_id')}",
                              "available": t.get("available"), "price": t.get("price"),
                              "lead_days": t.get("lead_days"),
                              "quoted_at": q.get("quoted_at"),
                              "provenance": t.get("provenance")})
        if t.get("lead_days") is not None:
            lead_values.append(int(t["lead_days"]))
        if t.get("price") is not None:
            price_values.append(float(t["price"]))
        if t.get("available") is not None and avail is None:
            avail = t.get("available")
        latest_quote = q

    price = (catalog.get("price") if catalog.get("price") is not None
             else (latest_quote or {}).get("terms", {}).get("price"))
    lead_days = max(lead_values) if lead_values else None  # 保守：取最长在效交期
    return {"available": avail, "price": price,
            "currency": catalog.get("currency", "CNY"),
            "lead_days": lead_days, "lead_days_values": lead_values,
            "terms_history": terms_history, "latest_quote": latest_quote}


# ---------------------------------------------------------------------------
# 单个直接候选评估
# ---------------------------------------------------------------------------

def evaluate_direct_option(store, order: dict[str, Any], asset: dict[str, Any] | None,
                           listing: dict[str, Any], as_of_date) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    evidence = [listing_evidence(listing)]

    mapping = listing.get("mapping") or {}
    mstatus = mapping.get("status", PENDING)

    # 与订单所属设备的直接符合性（独立于通用映射结论）
    fit = None
    if asset is not None:
        fit = evaluate_identity(listing.get("spec") or {}, asset.get("spec") or {})
        for r in fit["reasons"]:
            entry = {"code": r["code"], "severity": "blocker" if "conflict" in r["code"] else "hold",
                     "message": r["message"], "suggested_action": suggested_action(r["code"]),
                     "attribute": r.get("attribute")}
            if "conflict" in r["code"]:
                blockers.append(entry)
            else:
                holds.append(entry)
        evidence.append({"kind": "asset", "asset_id": asset.get("id"),
                         "model": asset.get("model"), "equipment_model": asset.get("equipment_model"),
                         "serial": asset.get("serial")})

    # 治理映射状态
    if mstatus == "rejected":
        blockers.append(_finding("manufacturer_code_mismatch"))
    elif mstatus == PENDING:
        # 若 fit 已给出同类原因则不重复；否则用映射原因
        fit_codes = {(h["code"], h.get("attribute")) for h in holds + blockers}
        for r in mapping.get("reasons", []):
            sig = (r["code"], r.get("attribute"))
            if sig in fit_codes:
                continue
            holds.append({"code": r["code"], "severity": "hold", "message": r["message"],
                          "suggested_action": suggested_action(r["code"]),
                          "attribute": r.get("attribute")})
        if not mapping.get("reasons"):
            holds.append(_finding("insufficient_identity_evidence", severity="hold"))

    comp = commercial_position(listing)
    qty = int(order.get("qty") or 1)
    if comp["available"] is None:
        warnings.append(_finding("availability_unknown", severity="warning"))
    elif comp["available"] <= 0:
        blockers.append(_finding("out_of_stock"))
    elif comp["available"] < qty:
        blockers.append(_finding("insufficient_quantity", available=comp["available"], required=qty))

    # 公告核对（停产 + 交期）。只有公告“主型号”就是该候选型号时才直接阻断；
    # 仅因“它是某停产型号的替代型号”而关联到的公告不在此阻断替代件本身。
    from .specs import model_key as _mk
    own_key = (listing.get("spec") or {}).get("model_key") or _mk(listing.get("model"))
    anns = effective_announcements(store, model=listing.get("model"), as_of_date=as_of_date)
    anns = [a for a in anns
            if a.get("kind") in ("discontinuation", "lead_time", "replacement", "compatibility")
            and a.get("model_key") == own_key]
    effective_lead = comp["lead_days"]
    earliest_delivery = None
    for ann in anns:
        evidence.append(announcement_evidence(ann))
        if ann.get("kind") == "discontinuation":
            blockers.append(_finding(
                "discontinued_effective",
                effective_date=ann.get("effective_date"),
                announcement_id=ann.get("id")))
        elif ann.get("kind") == "lead_time":
            if ann.get("lead_days") is not None:
                effective_lead = max(x for x in [effective_lead, ann["lead_days"]] if x is not None)
            if ann.get("earliest_delivery_date"):
                earliest_delivery = _max_date(earliest_delivery, ann["earliest_delivery_date"])

    due = parse_date(order.get("due"))
    arrival = add_days(as_of_date, effective_lead) if effective_lead is not None else None
    if earliest_delivery:
        ed = parse_date(earliest_delivery)
        if ed and (arrival is None or ed > arrival):
            arrival = ed
    if due and arrival and arrival > due:
        blockers.append(_finding("lead_time_exceeds_due",
                                 arrival=arrival.isoformat(), due=due.isoformat(),
                                 lead_days=effective_lead))

    releasable = not blockers and not holds
    verdict = RELEASABLE if releasable else (BLOCKED if blockers else PENDING)
    return {
        "kind": "direct",
        "lkey": listing.get("key"), "source_id": listing.get("source_id"),
        "item": listing.get("item"), "model": listing.get("model"),
        "mapping_status": mstatus,
        "fit_status": (fit or {}).get("status"),
        "fit_reasons": (fit or {}).get("reasons", []),
        "commercial": {k: v for k, v in comp.items() if k != "terms_history"},
        "terms_history": comp["terms_history"],
        "effective_lead_days": effective_lead,
        "projected_arrival": arrival.isoformat() if arrival else None,
        "due": due.isoformat() if due else None,
        "announcement_ids": [a["id"] for a in anns],
        "blockers": blockers, "holds": holds, "warnings": warnings,
        "evidence": evidence,
        "releasable": releasable, "verdict": verdict,
        "score": _option_score(comp, arrival, due),
    }


def _max_date(a: Any, b: Any) -> str:
    da, db = parse_date(a), parse_date(b)
    if da is None:
        return db.isoformat()
    if db is None:
        return da.isoformat()
    return max(da, db).isoformat()


def _option_score(comp: dict[str, Any], arrival, due) -> tuple:
    """排序：可满足交期优先、有库存优先、价格低优先。"""
    due_ok = 1 if (due is None or arrival is None or arrival <= due) else 0
    in_stock = 1 if (comp.get("available") or 0) > 0 else 0
    price = comp.get("price")
    return (due_ok, in_stock, -(price if price is not None else 10**12))


# ---------------------------------------------------------------------------
# 替代审批范围核对
# ---------------------------------------------------------------------------

def equipment_context(asset: dict[str, Any] | None) -> set[str]:
    if not asset:
        return set()
    tokens = {asset.get("id"), asset.get("asset"), asset.get("equipment_model"),
              asset.get("model")}
    return {str(t).strip() for t in tokens if t not in (None, "")}


def check_approval_scope(approval: dict[str, Any], asset: dict[str, Any] | None,
                         candidate_listing: dict[str, Any] | None) -> dict[str, Any]:
    """严格核对设备型号范围 + 参数范围。返回每一项的覆盖详情。"""
    ctx = equipment_context(asset)
    approved_equip = [str(x) for x in approval.get("equipment_models", []) if str(x).strip()]
    approved_assets = [str(x) for x in approval.get("asset_ids", []) if str(x).strip()]

    equipment_detail = {"approved": False, "context": sorted(ctx),
                        "equipment_models": approved_equip, "asset_ids": approved_assets}
    if not approved_equip and not approved_assets:
        equipment_detail["reason"] = "substitution_scope_incomplete"
    else:
        norm_ctx = {c.lower() for c in ctx}
        hit_e = next((e for e in approved_equip if e.lower() in norm_ctx), None)
        hit_a = next((a for a in approved_assets if a in ctx or a.lower() in norm_ctx), None)
        if hit_e or hit_a:
            equipment_detail["approved"] = True
            equipment_detail["matched"] = hit_e or hit_a
        else:
            equipment_detail["reason"] = "substitution_equipment_not_approved"

    ranges = approval.get("parameter_ranges") or {}
    parameter_detail: dict[str, Any] = {"approved": True, "checks": [], "ranges": ranges}
    if not ranges:
        parameter_detail["approved"] = False
        parameter_detail["reason"] = "substitution_scope_incomplete"
    else:
        cand_attrs = {}
        if candidate_listing:
            spec = candidate_listing.get("spec") or {}
            cand_attrs.update(spec.get("params", {}))
            cand_attrs.update(spec.get("dimensions", {}))
        asset_attrs = {}
        if asset:
            aspec = asset.get("spec") or {}
            asset_attrs.update(aspec.get("params", {}))
            asset_attrs.update(aspec.get("dimensions", {}))

        for name, rng in ranges.items():
            # 1) 需求值必须被范围覆盖
            req = asset_attrs.get(name)
            if req is not None:
                cov_req = value_covers(name, req, rng)
                parameter_detail["checks"].append(
                    {"parameter": name, "side": "requirement", **cov_req})
                if not cov_req.get("covered"):
                    parameter_detail["approved"] = False
            # 2) 替代件实际值必须落在批准范围内
            cand = cand_attrs.get(name)
            if cand is None:
                parameter_detail["checks"].append(
                    {"parameter": name, "side": "candidate", "covered": False,
                     "reason": "candidate_parameter_missing"})
                parameter_detail["approved"] = False
            else:
                cov_cand = value_covers(name, cand, rng)
                parameter_detail["checks"].append(
                    {"parameter": name, "side": "candidate", **cov_cand})
                if not cov_cand.get("covered"):
                    parameter_detail["approved"] = False

    return {"equipment": equipment_detail, "parameters": parameter_detail,
            "both_approved": equipment_detail["approved"] and parameter_detail["approved"]}


def evaluate_substitution_options(store, order, asset, as_of_date) -> list[dict[str, Any]]:
    required_keys = set()
    if asset and asset.get("model"):
        from .specs import model_key
        required_keys.add(model_key(asset["model"]))
    intended = find_item_listings(store, order.get("item"), order.get("source"))
    for ln in intended:
        if ln.get("spec", {}).get("model_key"):
            required_keys.add(ln["spec"]["model_key"])

    options = []
    for ap in store.approvals.values():
        if not approval_is_active(ap, as_of_date=as_of_date):
            continue
        from_key = ap.get("from_model_key")
        if required_keys and from_key and from_key not in required_keys:
            # 也允许 from_material 关联键
            fmat = store.materials.get(ap.get("from_material_id"))
            if not fmat or fmat.get("model_key") not in required_keys:
                continue
        target_key = ap.get("to_model_key")
        target_listings = [ln for ln in store.listings.values()
                           if ln.get("spec", {}).get("model_key") == target_key]
        if not target_listings and ap.get("to_material_id"):
            target_listings = [ln for ln in store.listings.values()
                               if ln.get("material_id") == ap["to_material_id"]]
        for ln in target_listings:
            scope = check_approval_scope(ap, asset, ln)
            base = evaluate_direct_option(store, order, asset, ln, as_of_date)
            # 替代是“有条件、按批准范围”的：凡在 parameter_ranges 中列明的属性，
            # 以 scope 的范围判定为准，不再用“必须与设备原值完全相等”的身份冲突阻断；
            # 未列入批准范围的属性差异/缺失仍按保守的 exact-fit 规则处理。
            governed = set((ap.get("parameter_ranges") or {}).keys())
            # 审批已显式建立 from_model -> to_model 的跨型号有条件替代关系，
            # 因此所有“编码/名称身份”类结论在替代路径下都不再作为独立阻碍；
            # 未被 parameter_ranges 治理的具体属性差异/缺失仍保留。
            generic_identity_codes = {
                "manufacturer_code_mismatch", "model_case_mismatch",
                "similarity_not_evidence", "unknown_manufacturer_model",
                "ambiguous_multiple_matches", "insufficient_identity_evidence"}
            attr_specific_codes = {
                "missing_attribute", "missing_parameter", "missing_dimension",
                "incomparable_attribute"}
            blockers = []
            for b in base["blockers"]:
                if (b.get("code") in ("parameter_conflict", "dimension_conflict")
                        and b.get("attribute") in governed):
                    continue
                blockers.append(b)
            holds = []
            for h in base["holds"]:
                code = str(h.get("code", ""))
                if code in generic_identity_codes:
                    continue  # 审批本身即有条件治理确认
                if code in attr_specific_codes and h.get("attribute") in governed:
                    continue  # 由 scope 范围检查权威判定（含“替代件缺该参数”）
                holds.append(h)
            scope_findings = []
            if not scope["equipment"]["approved"]:
                code = scope["equipment"].get("reason", "substitution_equipment_not_approved")
                scope_findings.append(_finding(
                    code, approval_id=ap["id"],
                    equipment_model=asset.get("equipment_model") or asset.get("id")))
            if not scope["parameters"]["approved"]:
                failed = [c for c in scope["parameters"]["checks"] if not c.get("covered")]
                first = failed[0] if failed else {}
                if first.get("reason") in ("candidate_parameter_missing", "required_value_missing"):
                    scope_findings.append(_finding(
                        "missing_parameter", severity="hold",
                        name=first.get("parameter", "?")))
                elif scope["parameters"].get("reason") == "substitution_scope_incomplete" or not ap.get("parameter_ranges"):
                    scope_findings.append(_finding("substitution_scope_incomplete"))
                else:
                    scope_findings.append(_finding(
                        "substitution_parameter_out_of_range",
                        approval_id=ap["id"], name=first.get("parameter", "?"),
                        value=first.get("reason", ""), range=ap.get("parameter_ranges")))
            release_blockers = blockers + [f for f in scope_findings]
            releasable = not release_blockers and not holds and scope["both_approved"]
            options.append({
                "kind": "approved_substitution",
                "approval_id": ap["id"],
                "approval_scope": scope,
                "lkey": ln.get("key"), "source_id": ln.get("source_id"),
                "item": ln.get("item"), "model": ln.get("model"),
                "commercial": base["commercial"],
                "effective_lead_days": base["effective_lead_days"],
                "projected_arrival": base["projected_arrival"],
                "announcement_ids": base["announcement_ids"],
                "blockers": release_blockers, "holds": holds,
                "warnings": base["warnings"],
                "evidence": base["evidence"] + [{
                    "kind": "approval", "approval_id": ap["id"],
                    "equipment_models": ap.get("equipment_models"),
                    "parameter_ranges": ap.get("parameter_ranges"),
                    "status": ap.get("status")}],
                "releasable": releasable,
                "verdict": RELEASABLE if releasable else (BLOCKED if release_blockers else PENDING),
                "score": base["score"],
            })
    options.sort(key=lambda o: o["score"], reverse=True)
    return options


def evaluate_same_material_alternatives(store, order, asset, intended_listings, as_of_date
                                        ) -> list[dict[str, Any]]:
    """同一已确认物料在其他经销商处的可选来源（建议性质，不改写订单结论）。"""
    intended_keys = {ln.get("key") for ln in intended_listings}
    model_keys = set()
    material_ids = set()
    if asset and asset.get("spec", {}).get("model_key"):
        model_keys.add(asset["spec"]["model_key"])
    for ln in intended_listings:
        if ln.get("spec", {}).get("model_key"):
            model_keys.add(ln["spec"]["model_key"])
        if ln.get("material_id"):
            material_ids.add(ln["material_id"])
    for mid in list(material_ids):
        mat = store.materials.get(mid)
        if mat and mat.get("model_key"):
            model_keys.add(mat["model_key"])

    alts = []
    for ln in store.listings.values():
        if ln.get("key") in intended_keys:
            continue
        if (ln.get("spec", {}).get("model_key") in model_keys
                or ln.get("material_id") in material_ids):
            opt = evaluate_direct_option(store, order, asset, ln, as_of_date)
            # 仅推荐治理/符合性通过的同源物料
            if opt["mapping_status"] == "confirmed" and opt["releasable"]:
                opt["kind"] = "same_material_alternative"
                alts.append(opt)
    alts.sort(key=lambda o: o["score"], reverse=True)
    return alts


# ---------------------------------------------------------------------------
# 订单级核对
# ---------------------------------------------------------------------------

def check_order(store, order_id: str, *, as_of_date=None) -> dict[str, Any]:
    if order_id not in store.orders:
        raise KeyError(f"未知订单: {order_id}")
    order = store.orders[order_id]
    if as_of_date is None:
        as_of_date = utc_now().date()
    else:
        as_of_date = parse_date(as_of_date) or utc_now().date()

    asset = store.assets.get(order.get("asset")) if order.get("asset") else None
    direct_listings = find_item_listings(store, order.get("item"), order.get("source"))

    candidate_options = [evaluate_direct_option(store, order, asset, ln, as_of_date)
                         for ln in direct_listings]
    substitution_options = evaluate_substitution_options(store, order, asset, as_of_date)
    same_material_alternatives = evaluate_same_material_alternatives(
        store, order, asset, direct_listings, as_of_date)

    releasable_direct = sorted([o for o in candidate_options if o["releasable"]],
                               key=lambda o: o["score"], reverse=True)
    releasable_sub = [o for o in substitution_options if o["releasable"]]

    recommended = None
    release_kind = None
    chosen = None
    if releasable_direct:
        chosen = releasable_direct[0]
        release_kind = "direct"
        recommended = {"kind": "direct", "lkey": chosen["lkey"],
                       "source_id": chosen["source_id"], "item": chosen["item"]}
    elif releasable_sub:
        chosen = releasable_sub[0]
        release_kind = "approved_substitution"
        recommended = {"kind": "approved_substitution",
                       "approval_id": chosen["approval_id"], "lkey": chosen["lkey"],
                       "source_id": chosen["source_id"], "item": chosen["item"]}

    all_blockers = [b for o in candidate_options + substitution_options for b in o["blockers"]]
    all_holds = [h for o in candidate_options for h in o["holds"]]
    if asset is None:
        all_blockers.append({"code": "asset_not_found", "severity": "blocker",
                             "message": f"订单关联设备 {order.get('asset')} 不存在，无法核对参数",
                             "suggested_action": "补建设备/物料事实后再核对。"})

    can_release = chosen is not None
    if can_release:
        verdict = RELEASABLE
    elif any(o["verdict"] == BLOCKED for o in candidate_options) or all_blockers:
        verdict = BLOCKED
    else:
        verdict = PENDING

    if can_release and chosen is not None:
        # 顶层 blockers/holds 描述“被推荐的合规路径”；被否候选的问题保留在各 options 中。
        top_blockers = _dedupe_findings(chosen.get("blockers", []))
        top_holds = _dedupe_findings(chosen.get("holds", []))
    else:
        top_blockers = _dedupe_findings(all_blockers)
        top_holds = _dedupe_findings(all_holds)

    evidence: list[dict[str, Any]] = []
    for o in candidate_options + substitution_options:
        for e in o.get("evidence", []):
            if e not in evidence:
                evidence.append(e)

    result = {
        "order_id": order_id,
        "as_of": as_of_date.isoformat(),
        "state": order.get("state"),
        "asset": ({"id": asset.get("id"), "model": asset.get("model"),
                   "equipment_model": asset.get("equipment_model"),
                   "load": (asset.get("raw") or {}).get("load"),
                   "serial": asset.get("serial")} if asset else None),
        "due": order.get("due"),
        "qty": order.get("qty", 1),
        "candidate_options": candidate_options,
        "substitution_options": substitution_options,
        "same_material_alternatives": same_material_alternatives,
        "recommended": recommended,
        "release_kind": release_kind,
        "can_release": can_release,
        "verdict": verdict,
        "blockers": top_blockers,
        "holds": top_holds,
        "evidence": evidence,
        "checked_at": iso(utc_now()),
    }
    result["decision_signature"] = _decision_signature(result)
    return result


def _dedupe_findings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for it in items:
        sig = (it.get("code"), it.get("attribute"), it.get("approval_id"),
               str(it.get("message"))[:40])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# 已确认订单：不可变快照 + 风险扫描
# ---------------------------------------------------------------------------

def _snapshot_fingerprint(check: dict[str, Any], order: dict[str, Any]) -> dict[str, Any]:
    from .common import content_hash
    core = {"order_id": order["order_id"], "item": order.get("item"),
            "source": order.get("source"), "due": order.get("due"),
            "qty": order.get("qty"), "recommended": check.get("recommended"),
            "release_kind": check.get("release_kind"), "verdict": check.get("verdict")}
    return {"core": core, "checksum": content_hash(core)}


def confirm_order(store, order_id: str, *, by: str = "buyer", at=None,
                  force: bool = False, waiver: str = "", as_of_date=None) -> dict[str, Any]:
    if order_id not in store.orders:
        raise KeyError(order_id)
    order = store.orders[order_id]
    check = check_order(store, order_id, as_of_date=as_of_date)
    if not check["can_release"] and not force:
        return {"confirmed": False, "reason": "pre_purchase_check_failed", "check": check}

    ts = iso(at or utc_now())
    fp = _snapshot_fingerprint(check, order)
    order["state"] = CONFIRMED_ORDER
    order["snapshot"] = {
        "confirmed_at": ts, "confirmed_by": by,
        "header": {k: order.get(k) for k in ("order_id", "asset", "item", "source", "due", "qty")},
        "verdict_at_confirmation": check["verdict"],
        "can_release_at_confirmation": check["can_release"],
        "release_kind": check["release_kind"],
        "recommended": check["recommended"],
        "checksum": fp["checksum"],
        "fingerprint": fp["core"],
        "fact_version_ids": [e.get("version_id") for e in check["evidence"] if e.get("version_id")],
        "announcement_ids_checked": sorted({e.get("announcement_id") for e in check["evidence"]
                                            if e.get("kind") == "announcement" and e.get("announcement_id")}),
        "approval_ids_checked": sorted({e.get("approval_id") for e in check["evidence"]
                                        if e.get("kind") == "approval" and e.get("approval_id")}),
        "forced": bool(force and not check["can_release"]),
        "waiver": waiver,
    }
    order["original_decision"] = {
        "verdict": check["verdict"], "recommended": check["recommended"],
        "release_kind": check["release_kind"], "at": ts, "by": by}
    store.audit("order.confirmed", actor=by, order=order_id,
                forced=order["snapshot"]["forced"], checksum=fp["checksum"])
    store.save() if store.state_path else None
    return {"confirmed": True, "check": check, "snapshot": order["snapshot"]}


def ensure_imported_baseline(store, *, now=None) -> int:
    """对导入时已是 confirmed 但没有快照的历史订单建立 imported 基线。"""
    n = 0
    for oid, order in store.orders.items():
        if order.get("state") in ("confirmed",) and not order.get("snapshot"):
            check = check_order(store, oid)
            fp = _snapshot_fingerprint(check, order)
            order["snapshot"] = {
                "confirmed_at": order.get("imported_at") or iso(now or utc_now()),
                "confirmed_by": "imported", "imported": True,
                "header": {k: order.get(k) for k in ("order_id", "asset", "item", "source", "due", "qty")},
                "verdict_at_confirmation": check["verdict"],
                "can_release_at_confirmation": check["can_release"],
                "release_kind": check["release_kind"],
                "recommended": check["recommended"],
                "checksum": fp["checksum"], "fingerprint": fp["core"],
                "fact_version_ids": [e.get("version_id") for e in check["evidence"] if e.get("version_id")],
                "announcement_ids_checked": sorted({a for a in check_order_ann_ids(check)}),
                "approval_ids_checked": [],
            }
            order["original_decision"] = {
                "verdict": check["verdict"], "recommended": check["recommended"],
                "release_kind": check["release_kind"],
                "at": order["snapshot"]["confirmed_at"], "by": "imported"}
            n += 1
    return n


def check_order_ann_ids(check: dict[str, Any]) -> list[str]:
    return [e.get("announcement_id") for e in check.get("evidence", [])
            if e.get("kind") == "announcement" and e.get("announcement_id")]


def scan_confirmed_order_risks(store, order_id: str, *, as_of_date=None) -> dict[str, Any]:
    """在不动原决策的前提下，重算已确认订单的当前风险。"""
    order = store.orders[order_id]
    snapshot = order.get("snapshot") or {}
    check = check_order(store, order_id, as_of_date=as_of_date)
    known_anns = set(snapshot.get("announcement_ids_checked", []))
    known_approvals = set(snapshot.get("approval_ids_checked", []))
    risks: list[dict[str, Any]] = []

    # 先识别“新公告覆盖交付日期”，以便把同源的通用交期阻断合并为更具体的复核事项。
    due = parse_date(order.get("due"))
    as_of = parse_date(as_of_date) or utc_now().date()
    delivery_override = False
    delivery_risks: list[dict[str, Any]] = []
    for ann_id in check_order_ann_ids(check):
        ann = store.announcements.get(ann_id)
        if not ann:
            continue
        is_new = ann_id not in known_anns
        opt = next((o for o in check["candidate_options"]
                    if ann_id in o.get("announcement_ids", [])), None)
        arrival = parse_date(opt.get("projected_arrival")) if opt else None
        overrides_due = bool(due and arrival and arrival > due)
        explicit = parse_date(ann.get("earliest_delivery_date"))
        if due and explicit and explicit > due:
            overrides_due = True
        if ann.get("kind") in ("lead_time", "discontinuation") and overrides_due:
            delivery_override = True
            delivery_risks.append({
                "code": "delivery_date_superseded",
                "severity": "blocker",
                "new_announcement": is_new,
                "announcement_id": ann_id,
                "message": reason_text("delivery_date_superseded",
                                        announcement_id=ann_id, due=order.get("due")),
                "suggested_action": suggested_action("delivery_date_superseded"),
                "evidence": [announcement_evidence(ann)] + (
                    [listing_evidence(store.listings[opt["lkey"]])] if opt and opt.get("lkey") in store.listings else []),
                "dates": {"due": due.isoformat(),
                          "projected_arrival": arrival.isoformat() if arrival else None,
                          "announcement_effective_date": ann.get("effective_date"),
                          "earliest_delivery_date": ann.get("earliest_delivery_date")},
            })

    for opt in check["candidate_options"]:
        for b in opt["blockers"]:
            # 已生成更具体的“交付日期被覆盖”复核事项时，合并同源交期阻断
            if b["code"] == "lead_time_exceeds_due" and delivery_override:
                continue
            risks.append(_risk_from_code(b["code"], b, opt, check, known_anns))
        for h in opt["holds"]:
            risks.append(_risk_from_code(h["code"], h, opt, check, known_anns, severity="warning"))

    risks.extend(delivery_risks)

    # 映射被更正 / 确认后冲突
    order_lkeys = {ln.get("key") for ln in find_item_listings(store, order.get("item"), order.get("source"))}
    confirmed_at = snapshot.get("confirmed_at") or ""
    correction_events = [
        e for e in store.audit_events
        if e.get("action") == "mapping.corrected"
        and e.get("details", {}).get("listing") in order_lkeys
        and (e.get("at") or "") >= confirmed_at
    ]
    for evt in correction_events:
        d = evt.get("details", {})
        risks.append({
            "code": "mapping_corrected", "severity": "warning",
            "new_announcement": False,
            "correction": {"at": evt.get("at"), "by": evt.get("actor"),
                           "from_material": d.get("from"), "to_material": d.get("to")},
            "message": reason_text("mapping_corrected"),
            "suggested_action": suggested_action("mapping_corrected"),
            "lkey": d.get("listing"),
            "evidence": [{"kind": "mapping_correction", **d, "at": evt.get("at"),
                          "by": evt.get("actor")}],
        })

    for ln in find_item_listings(store, order.get("item"), order.get("source")):
        m = ln.get("mapping") or {}
        if m.get("status") in (PENDING, "rejected") and snapshot.get("verdict_at_confirmation") == RELEASABLE:
            code = "mapping_corrected" if (ln.get("manual_decision") or {}).get("at", "") >= (
                snapshot.get("confirmed_at") or "") else "new_conflict_after_confirmation"
            risks.append({
                "code": code, "severity": "warning",
                "message": reason_text(code),
                "suggested_action": suggested_action(code),
                "lkey": ln.get("key"),
                "evidence": [listing_evidence(ln)],
            })

    # 使用过的替代审批被撤回
    rec = snapshot.get("recommended") or {}
    if rec.get("kind") == "approved_substitution" and rec.get("approval_id"):
        ap = store.approvals.get(rec["approval_id"])
        if ap and not approval_is_active(ap, as_of_date=as_of):
            risks.append({
                "code": "approval_withdrawn", "severity": "blocker",
                "approval_id": ap["id"],
                "message": reason_text("approval_withdrawn", approval_id=ap["id"]),
                "suggested_action": suggested_action("approval_withdrawn"),
                "evidence": [{"kind": "approval", "approval_id": ap["id"],
                              "status": ap.get("status")}],
            })

    # 去重
    dedup = _dedupe_findings(risks)
    order["current_risks"] = dedup
    order["current_assessment"] = {
        "as_of": check["as_of"], "verdict": check["verdict"],
        "can_release": check["can_release"], "scanned_at": iso(utc_now())}
    return {"order_id": order_id, "original_decision": order.get("original_decision"),
            "snapshot_preserved": bool(snapshot), "risks": dedup, "check": check}


def _risk_from_code(code: str, finding: dict, option: dict, check: dict,
                    known_anns: set, *, severity: str | None = None) -> dict[str, Any]:
    ann_evidence = [e for e in option.get("evidence", []) if e.get("kind") == "announcement"]
    new_ann = any(e.get("announcement_id") not in known_anns for e in ann_evidence)
    return {
        "code": code,
        "severity": severity or finding.get("severity", "warning"),
        "new_announcement": new_ann,
        "message": finding.get("message") or reason_text(code),
        "suggested_action": finding.get("suggested_action") or suggested_action(code),
        "lkey": option.get("lkey"),
        "evidence": option.get("evidence", []),
    }


# ---------------------------------------------------------------------------
# 未下单需求重算
# ---------------------------------------------------------------------------

def _decision_signature(check: dict[str, Any]) -> str:
    import json
    codes = sorted({b.get("code") for b in check.get("blockers", [])}
                   | {h.get("code") for h in check.get("holds", [])})
    rec = check.get("recommended") or {}
    return json.dumps({"v": check.get("verdict"), "codes": codes,
                       "rec": rec.get("lkey") or rec.get("approval_id")},
                      ensure_ascii=False, sort_keys=True)


def recompute_open_requirements(store, *, trigger: str = "manual", as_of_date=None,
                                now=None) -> dict[str, Any]:
    affected = []
    for oid, order in sorted(store.orders.items()):
        if order.get("state") not in ORDER_OPEN_STATES:
            continue
        prev = order.get("last_check") or {}
        before = prev.get("verdict")
        before_sig = prev.get("decision_signature")
        check = check_order(store, oid, as_of_date=as_of_date)
        signature = _decision_signature(check)
        check["decision_signature"] = signature
        order["last_check"] = check
        after = check["verdict"]
        changed = before != after or before_sig != signature
        affected.append({"order_id": oid, "before": before, "after": after,
                         "changed": changed,
                         "signature": signature,
                         "can_release": check["can_release"],
                         "recommended": check.get("recommended")})
    record = {
        "id": store.new_id("REC"), "at": iso(now or utc_now()), "trigger": trigger,
        "as_of": (parse_date(as_of_date) or utc_now().date()).isoformat(),
        "affected": affected,
        "changed_count": sum(1 for a in affected if a["changed"]),
    }
    store.data["recomputations"].append(record)
    store.audit("requirements.recomputed", trigger=trigger,
                changed=record["changed_count"], total=len(affected))
    return record
