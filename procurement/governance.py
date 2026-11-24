"""物料治理：规范物料、经销商编码映射核验、替代审批。

映射结论分三类：

* ``confirmed`` 已确认（必须有可靠编码关系 + 至少一项匹配的尺寸/额定参数 + 无冲突，
  或治理人员/有效审批明确确认）；
* ``pending`` 待核（信息不足、参数/尺寸冲突、仅相似、大小写歧义等）；
* ``rejected`` 已排除（治理人员明确否决）。

相似度只用于展示，绝不参与放行。
"""
from __future__ import annotations

from typing import Any

from .common import deterministic_id, iso, utc_now
from .specs import build_spec, evaluate_identity, model_key, model_relation
from .status import CONFIRMED, PENDING, REJECTED


def listing_key(source_id: str, item: str) -> str:
    return f"{source_id}::{item}"


def material_id_for_model(model: str | None) -> str:
    key = model_key(model) or "UNKNOWN"
    return f"MAT-{key}"


# ---------------------------------------------------------------------------
# 物料
# ---------------------------------------------------------------------------

def ensure_material(store, *, model: str | None = None, spec: dict[str, Any] | None = None,
                    manufacturer: str | None = None) -> dict[str, Any]:
    spec = spec or build_spec(model=model, manufacturer=manufacturer)
    model = spec.get("model") or model
    mid = material_id_for_model(model)
    mat = store.materials.get(mid)
    if mat is None:
        mat = {
            "id": mid,
            "canonical_model": model,
            "model_key": spec.get("model_key") or model_key(model),
            "manufacturer": spec.get("manufacturer") or manufacturer,
            "category": spec.get("category"),
            "spec": spec,
            "codes": sorted({model} if model else set()),
            "governance_status": PENDING,
            "manual_status": None,
            "authoritative_spec": None,
            "created_at": iso(utc_now()),
            "updated_at": iso(utc_now()),
        }
        store.materials[mid] = mat
    else:
        if model and model not in mat["codes"]:
            mat["codes"].append(model)
        if spec.get("manufacturer") and not mat.get("manufacturer"):
            mat["manufacturer"] = spec["manufacturer"]
        if spec.get("category") and not mat.get("category"):
            mat["category"] = spec["category"]
        mat["updated_at"] = iso(utc_now())
    return mat


def find_material_by_model(store, model: str) -> dict[str, Any] | None:
    return store.materials.get(material_id_for_model(model))


def resolve_material_identifier(store, identifier: str) -> dict[str, Any] | None:
    """允许用 material id、厂商型号、model_key 或经销商编码定位物料。"""
    if not identifier:
        return None
    ident = str(identifier)
    if ident in store.materials:
        return store.materials[ident]
    # 直接型号 / 键
    mid = material_id_for_model(ident)
    if mid in store.materials:
        return store.materials[mid]
    key = model_key(ident)
    for mat in store.materials.values():
        if mat.get("model_key") == key:
            return mat
    # 经销商编码 source::item 或全局 item
    key2 = ident if "::" in ident else None
    if key2 and key2 in store.listings:
        return store.materials.get(store.listings[key2].get("material_id"))
    matches = [ln for ln in store.listings.values() if ln.get("item") == ident]
    if len(matches) == 1:
        return store.materials.get(matches[0].get("material_id"))
    return None


# ---------------------------------------------------------------------------
# 参考证据与映射评估
# ---------------------------------------------------------------------------

def _asset_spec(asset: dict[str, Any]) -> dict[str, Any]:
    if asset.get("spec"):
        return asset["spec"]
    return build_spec(
        model=asset.get("model"), manufacturer=asset.get("manufacturer"),
        load=asset.get("load"), dimensions=asset.get("dimensions"),
        params=asset.get("params"), equipment_model=asset.get("equipment_model"))


def reference_assets_for(store, spec: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for asset in store.assets.values():
        aspec = _asset_spec(asset)
        rel = model_relation(spec.get("model"), aspec.get("model"))
        if rel in ("exact", "normalized_code"):
            out.append(asset)
    return out


def _consensus_spec(siblings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """当 >=2 个其他来源且属性一致时形成共识参考规格。"""
    usable = [ln.get("spec") for ln in siblings if ln.get("spec")]
    if len(usable) < 2:
        return None
    dims: dict[str, Any] = {}
    params: dict[str, Any] = {}
    for bucket_name, target in (("dimensions", dims), ("params", params)):
        names = set()
        for sp in usable:
            names.update(sp.get(bucket_name, {}).keys())
        for name in names:
            attrs = [sp[bucket_name][name] for sp in usable if name in sp.get(bucket_name, {})]
            if len(attrs) != len(usable):
                continue  # 有缺失，不形成强共识
            raws = {str(a.get("level", a.get("value"))) for a in attrs}
            if len(raws) == 1:
                target[name] = attrs[0]
    model = next((sp.get("model") for sp in usable if sp.get("model")), None)
    return {"model": model, "model_key": model_key(model),
            "dimensions": dims, "params": params}


def evaluate_listing_mapping(store, lkey: str) -> dict[str, Any]:
    """计算某经销商编码当前的治理映射结论（不考虑人工覆盖的自动部分）。"""
    listing = store.listings[lkey]
    spec = listing.get("spec") or build_spec(model=listing.get("model"))
    mid = listing.get("material_id") or material_id_for_model(spec.get("model"))
    mat = store.materials.get(mid)
    comparisons: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []
    reference = None
    auto_status = PENDING

    # 1) 权威物料规格（人工录入/确认）
    if mat and mat.get("authoritative_spec"):
        reference = {"kind": "authoritative", "id": mat["id"], "spec": mat["authoritative_spec"]}
        verdict = evaluate_identity(spec, mat["authoritative_spec"])
        comparisons = verdict["comparisons"]
        reasons = verdict["reasons"]
        auto_status = verdict["status"]
    else:
        # 2) 设备/资产事实（独立证据）
        assets = reference_assets_for(store, spec)
        asset_verdicts = []
        for asset in assets:
            aspec = _asset_spec(asset)
            v = evaluate_identity(spec, aspec)
            asset_verdicts.append((asset, v))
            comparisons.extend([{**c, "reference": f"asset:{asset['id']}"} for c in v["comparisons"]])

        conflict_assets = [(a, v) for a, v in asset_verdicts if v["has_conflict"]]
        confirm_assets = [(a, v) for a, v in asset_verdicts if v["status"] == CONFIRMED]
        if conflict_assets:
            # 以第一个冲突结论的原因作为主原因（保留全部）
            for a, v in conflict_assets:
                reasons.extend(v["reasons"])
            reference = {"kind": "asset", "id": conflict_assets[0][0]["id"]}
            auto_status = PENDING
        elif confirm_assets:
            v = confirm_assets[0][1]
            reasons = v["reasons"]
            reference = {"kind": "asset", "id": confirm_assets[0][0]["id"]}
            auto_status = CONFIRMED
        elif asset_verdicts:
            # 编码对得上但参数不足
            for a, v in asset_verdicts:
                reasons.extend(v["reasons"])
            reference = {"kind": "asset", "id": asset_verdicts[0][0]["id"]}
            auto_status = PENDING
        else:
            # 3) 同源码组的共识
            siblings = [ln for k, ln in store.listings.items()
                        if k != lkey and ln.get("spec", {}).get("model_key") == spec.get("model_key")]
            consensus = _consensus_spec(siblings)
            if consensus:
                v = evaluate_identity(spec, consensus)
                comparisons = v["comparisons"]
                reasons = v["reasons"]
                reference = {"kind": "consensus", "id": spec.get("model_key")}
                auto_status = v["status"]
            else:
                v = evaluate_identity(spec, {"model": spec.get("model"),
                                             "dimensions": {}, "params": {}})
                reasons = v["reasons"]
                auto_status = PENDING
                reference = {"kind": "none", "id": None}

    # 去重原因
    dedup: list[dict[str, Any]] = []
    seen = set()
    for r in reasons:
        sig = (r.get("code"), r.get("attribute"))
        if sig in seen:
            continue
        seen.add(sig)
        dedup.append(r)

    return {
        "status": auto_status,
        "reasons": dedup,
        "comparisons": comparisons,
        "reference": reference,
        "material_id": mid,
        "evaluated_at": iso(utc_now()),
    }


def refresh_listing_mapping(store, lkey: str) -> dict[str, Any]:
    """重新计算并写回 mapping，同时保留人工决定。"""
    listing = store.listings[lkey]
    auto = evaluate_listing_mapping(store, lkey)
    manual = listing.get("manual_decision")
    if manual and manual.get("status") in (CONFIRMED, REJECTED):
        # 人工决定优先，但扫描未被知悉的新冲突
        current_conflicts = [r for r in auto["reasons"]
                             if r["code"] in ("parameter_conflict", "dimension_conflict")]
        ack = set(map(tuple, manual.get("acknowledged_conflicts", [])))
        unacked = [r for r in current_conflicts
                   if (r["code"], r.get("attribute")) not in ack]
        mapping = {
            "status": manual["status"],
            "decided_by": "governance",
            "manual": True,
            "by": manual.get("by"),
            "decided_at": manual.get("at"),
            "note": manual.get("note"),
            "material_id": manual.get("material_id") or auto["material_id"],
            "auto_status": auto["status"],
            "reasons": auto["reasons"],
            "unacknowledged_conflicts": unacked,
            "reference": auto["reference"],
            "comparisons": auto["comparisons"],
            "updated_at": iso(utc_now()),
        }
        if manual["status"] == CONFIRMED and unacked and not manual.get("override_all_conflicts"):
            mapping["status"] = PENDING
            mapping["reasons"] = auto["reasons"] + [{
                "code": "unacknowledged_conflict",
                "message": "确认后出现尚未知悉的参数冲突，需重新复核。"}]
    else:
        mapping = {
            "status": auto["status"],
            "decided_by": "auto",
            "manual": False,
            "material_id": auto["material_id"],
            "reasons": auto["reasons"],
            "reference": auto["reference"],
            "comparisons": auto["comparisons"],
            "updated_at": iso(utc_now()),
        }
    listing["mapping"] = mapping
    # 同步物料治理状态（只要有任一映射待核，物料整体视为待核；人工物料状态优先）
    mat = store.materials.get(mapping["material_id"])
    if mat and not mat.get("manual_status"):
        statuses = [(ln.get("mapping") or {}).get("status") for ln in store.listings.values()
                    if ln.get("material_id") == mat["id"]]
        mat["governance_status"] = PENDING if PENDING in statuses else (
            REJECTED if all(s == REJECTED for s in statuses if s) else CONFIRMED)
    return mapping


def reevaluate_all_mappings(store) -> dict[str, str]:
    result = {}
    for lkey in list(store.listings.keys()):
        result[lkey] = refresh_listing_mapping(store, lkey)["status"]
    return result


def apply_manual_decision(store, lkey: str, status: str, *, by: str = "governance",
                          material_id: str | None = None, note: str = "",
                          acknowledge_conflicts: bool = False,
                          override_all_conflicts: bool = False) -> dict[str, Any]:
    if lkey not in store.listings:
        raise KeyError(f"未知经销商编码映射: {lkey}")
    listing = store.listings[lkey]
    auto = evaluate_listing_mapping(store, lkey)
    target_mid = material_id or listing.get("material_id") or auto["material_id"]
    if target_mid not in store.materials:
        # 允许直接指定新型号
        spec = listing.get("spec") or build_spec()
        mat = ensure_material(store, model=target_mid.replace("MAT-", ""), spec=spec)
        target_mid = mat["id"]
    ack: list[list[str]] = []
    if acknowledge_conflicts or override_all_conflicts:
        for r in auto["reasons"]:
            if r["code"] in ("parameter_conflict", "dimension_conflict"):
                ack.append([r["code"], r.get("attribute")])
    listing["material_id"] = target_mid
    listing["manual_decision"] = {
        "status": status, "by": by, "at": iso(utc_now()), "note": note,
        "material_id": target_mid,
        "acknowledged_conflicts": ack,
        "override_all_conflicts": bool(override_all_conflicts),
    }
    mapping = refresh_listing_mapping(store, lkey)
    store.audit("mapping.decision", actor=by, listing=lkey, status=status,
                material_id=target_mid, note=note)
    return mapping


# ---------------------------------------------------------------------------
# 替代审批
# ---------------------------------------------------------------------------

def add_approval(store, *, from_model: str | None = None, to_model: str | None = None,
                  from_material_id: str | None = None, to_material_id: str | None = None,
                  equipment_models: list[str] | None = None,
                  asset_ids: list[str] | None = None,
                  parameter_ranges: dict[str, Any] | None = None,
                  valid_from: Any = None, valid_to: Any = None,
                  approval_id: str | None = None, by: str = "governance",
                  note: str = "", status: str = "active",
                  now=None) -> dict[str, Any]:
    if from_material_id is None and from_model:
        from_material_id = ensure_material(store, model=from_model)["id"]
    if to_material_id is None and to_model:
        to_material_id = ensure_material(store, model=to_model)["id"]

    apid = approval_id or store.new_id("AP")
    approval = {
        "id": apid,
        "from_model": from_model or (store.materials.get(from_material_id, {}) or {}).get("canonical_model"),
        "to_model": to_model or (store.materials.get(to_material_id, {}) or {}).get("canonical_model"),
        "from_model_key": model_key(from_model) if from_model else (
            store.materials.get(from_material_id, {}) or {}).get("model_key"),
        "to_model_key": model_key(to_model) if to_model else (
            store.materials.get(to_material_id, {}) or {}).get("model_key"),
        "from_material_id": from_material_id,
        "to_material_id": to_material_id,
        "equipment_models": [str(x) for x in (equipment_models or [])],
        "asset_ids": [str(x) for x in (asset_ids or [])],
        "parameter_ranges": parameter_ranges or {},
        "valid_from": valid_from,
        "valid_to": valid_to,
        "status": status,
        "note": note,
        "created_by": by,
        "created_at": iso(now or utc_now()),
    }
    store.approvals[apid] = approval
    store.audit("approval.added", actor=by, approval=apid,
                **{"from": approval["from_model"], "to": approval["to_model"]})
    return approval


def approval_is_active(approval: dict[str, Any], *, as_of_date=None) -> bool:
    from .common import parse_date
    if approval.get("status") != "active":
        return False
    today = as_of_date
    vf = parse_date(approval.get("valid_from"))
    vt = parse_date(approval.get("valid_to"))
    if today is not None:
        if vf and vf > today:
            return False
        if vt and vt < today:
            return False
    return True
