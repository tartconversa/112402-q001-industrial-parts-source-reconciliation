"""采集入库：原始版本留存 + 规范化事实 + 来源证据链。

每条规范化事实（Listing/Quote/Announcement）的 ``provenance`` 都指向一条
不可变的 raw version，包含来源、抓取时间、来源更新时间、原始载荷哈希与
适配器版本，满足“可追溯来源、抓取时间和原始版本”。
"""
from __future__ import annotations

from typing import Any

from .adapters import adapt_announcement_row, adapt_row
from .announcements import upsert_announcement
from .common import content_hash, deterministic_id, iso, parse_datetime, utc_now
from .governance import ensure_material, refresh_listing_mapping
from .specs import build_spec
from .status import ORDER_PLACED_STATES

ADAPTER_VERSION = "1.0.0"


class IngestionService:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------
    # 原始版本
    # ------------------------------------------------------------------
    def persist_raw(self, *, source_id: str, record_key: str, payload: dict[str, Any],
                    run_id: str | None, fetched_at: str,
                    source_updated_raw: Any = None,
                    adapter: str | None = None) -> tuple[dict[str, Any], bool]:
        payload_hash = content_hash(payload)
        # 版本 ID 仅由“逻辑记录 + 载荷哈希”决定：同一原始版本跨运行/重启稳定且唯一。
        version_id = deterministic_id("RV", record_key, payload_hash)
        logical = self.store.data["raw_records"].get(record_key)

        def _build_version() -> dict[str, Any]:
            return {
                "version_id": version_id,
                "source_id": source_id,
                "record_key": record_key,
                "payload": payload,
                "payload_hash": payload_hash,
                "adapter": adapter,
                "adapter_version": ADAPTER_VERSION,
                "run_id": run_id,
                "fetched_at": fetched_at,
                "source_updated_at": iso(parse_datetime(source_updated_raw)),
                "source_updated_raw": source_updated_raw,
                "first_seen_at": fetched_at,
                "last_seen_at": fetched_at,
                "times_seen": 1,
            }

        if logical is None:
            version = _build_version()
            self.store.record_versions.append(version)
            self.store.data["raw_records"][record_key] = {
                "key": record_key, "head_version_id": version_id,
                "version_ids": [version_id], "head_hash": payload_hash,
                "first_seen_at": fetched_at, "last_seen_at": fetched_at, "times_seen": 1,
            }
            return version, True

        logical["times_seen"] = int(logical.get("times_seen", 1)) + 1
        logical["last_seen_at"] = fetched_at

        # 该载荷版本历史上已存在（即使不是当前 head）：不重复建版本，仅把 head 指回它。
        if version_id in logical.get("version_ids", []):
            version = next(v for v in self.store.record_versions
                           if v["version_id"] == version_id)
            version["last_seen_at"] = fetched_at
            version["times_seen"] = int(version.get("times_seen", 1)) + 1
            logical["head_version_id"] = version_id
            logical["head_hash"] = payload_hash
            return version, False

        version = _build_version()
        self.store.record_versions.append(version)
        logical.setdefault("version_ids", []).append(version_id)
        logical["head_version_id"] = version_id
        logical["head_hash"] = payload_hash
        return version, True

    # ------------------------------------------------------------------
    # Sink：供 sources.run_source 逐批调用
    # ------------------------------------------------------------------
    def persist_batch(self, *, source_id: str, records: list[dict[str, Any]],
                      run_id: str, fetched_at: str, adapter: str | None = None,
                      field_map: dict[str, str] | None = None) -> dict[str, Any]:
        cfg = self.store.sources.get(source_id, {})
        kind = cfg.get("kind", "catalog")
        use_adapter = adapter or cfg.get("adapter") or "generic"
        new_versions = 0
        adapted_items: list[str] = []
        for raw in records:
            if not isinstance(raw, dict):
                continue
            if kind == "announcement":
                n = self._ingest_announcement_raw(
                    raw, feed_source_id=source_id, run_id=run_id,
                    fetched_at=fetched_at, adapter=use_adapter, field_map=field_map)
                new_versions += int(n)
            else:
                lkey, is_new = self._ingest_offer_raw(
                    raw, feed_source_id=source_id, run_id=run_id,
                    fetched_at=fetched_at, adapter=use_adapter, field_map=field_map,
                    kind=kind)
                new_versions += int(is_new)
                if lkey:
                    adapted_items.append(lkey)
        # 受影响映射立即重算
        for lkey in adapted_items:
            if lkey in self.store.listings:
                refresh_listing_mapping(self.store, lkey)
        return {"new_versions": new_versions, "items": adapted_items}

    # ------------------------------------------------------------------
    # 供货/报价行
    # ------------------------------------------------------------------
    def _ingest_offer_raw(self, raw: dict[str, Any], *, feed_source_id: str,
                          run_id: str | None, fetched_at: str, adapter: str,
                          field_map: dict[str, str] | None,
                          kind: str = "catalog") -> tuple[str | None, bool]:
        obs = adapt_row(raw, source_id=feed_source_id, adapter=adapter, field_map=field_map)
        sid = obs.get("source_id") or feed_source_id
        item = obs.get("item")
        if not item:
            # 没有经销商编码时用载荷哈希作为定位，保证可留存但提示歧义
            item = f"ROW-{content_hash(raw)[:10]}"
        record_key = f"offer:{sid}:{item}"
        version, is_new = self.persist_raw(
            source_id=sid, record_key=record_key, payload=raw, run_id=run_id,
            fetched_at=fetched_at, source_updated_raw=obs.get("source_updated"),
            adapter=adapter)
        provenance = {
            "source_id": sid,
            "feed_source_id": feed_source_id,
            "kind": kind,
            "version_id": version["version_id"],
            "payload_hash": version["payload_hash"],
            "run_id": run_id,
            "fetched_at": fetched_at,
            "source_updated_at": version["source_updated_at"],
            "source_updated_raw": obs.get("source_updated"),
            "adapter": adapter,
            "adapter_version": ADAPTER_VERSION,
            "url": obs.get("url"),
        }
        lkey = f"{sid}::{item}"
        listing = self.store.listings.get(lkey)
        if listing is None:
            listing = {
                "key": lkey, "source_id": sid, "item": item,
                "name": obs.get("name"), "model": obs.get("model"),
                "spec": obs["spec"], "material_id": None,
                "catalog": None, "quotes": [], "observations": [],
                "source_differences": [], "mapping": None,
                "manual_decision": None,
                "created_at": fetched_at, "updated_at": fetched_at,
            }
            self.store.listings[lkey] = listing
        # 身份字段更新：合并 Spec，避免稀疏报价行抹掉目录已有的额定参数；
        # 显式冲突会记录到 spec_changes，供治理/风险扫描使用。
        from .specs import merge_spec
        listing["name"] = obs.get("name") or listing.get("name")
        if obs.get("model"):
            listing["model"] = obs["model"]
        merged, changes = merge_spec(listing.get("spec"), obs.get("spec"))
        listing["spec"] = merged
        if changes:
            listing.setdefault("spec_changes", []).append(
                {"at": fetched_at, "version_id": version["version_id"], "changes": changes})
        listing["updated_at"] = fetched_at

        ensure_material(self.store, spec=obs["spec"])
        if not listing.get("material_id"):
            from .governance import material_id_for_model
            listing["material_id"] = material_id_for_model(obs.get("model"))

        commercial = {
            "available": obs.get("available"),
            "price": obs.get("price"),
            "currency": obs.get("currency"),
            "lead_days": obs.get("lead_days"),
            "min_qty": obs.get("min_qty"),
            "provenance": provenance,
        }
        if obs.get("quote_id") or kind == "quote_import":
            qid = obs.get("quote_id") or deterministic_id("Q", sid, item,
                                                          obs.get("quoted_at_raw"),
                                                          version["payload_hash"], length=8)
            quote_record = {
                "quote_id": qid, "source_id": sid, "item": item,
                "model": obs.get("model"), "terms": commercial,
                "quoted_at": iso(parse_datetime(obs.get("quoted_at_raw"))) or fetched_at,
                "quoted_at_raw": obs.get("quoted_at_raw"),
                "valid_until": iso(parse_datetime(obs.get("valid_until_raw"))),
                "provenance": provenance,
            }
            # 同 quote_id 幂等覆盖（保留版本证据）
            self.store.quotes[qid] = quote_record
            existing_q = next((q for q in listing["quotes"] if q.get("quote_id") == qid), None)
            if existing_q is None:
                listing["quotes"].append(quote_record)
            else:
                existing_q.update(quote_record)
        else:
            listing["catalog"] = commercial

        listing["observations"].append({
            "at": fetched_at, "kind": kind, "version_id": version["version_id"],
            "changed": is_new,
        })
        return lkey, is_new

    def ingest_offer_rows(self, rows: list[dict[str, Any]], *, source_id: str,
                          kind: str = "catalog", adapter: str = "generic",
                          field_map: dict[str, str] | None = None,
                          fetched_at: Any = None) -> dict[str, Any]:
        fetched_iso = iso(fetched_at or utc_now())
        run_id = self.store.new_id("RUN")
        result = self.persist_batch(
            source_id=source_id, records=rows, run_id=run_id,
            fetched_at=fetched_iso, adapter=adapter, field_map=field_map)
        return result

    # ------------------------------------------------------------------
    # 公告
    # ------------------------------------------------------------------
    def _ingest_announcement_raw(self, raw: dict[str, Any], *, feed_source_id: str,
                                 run_id: str | None, fetched_at: str, adapter: str,
                                 field_map: dict[str, str] | None) -> bool:
        adapted = adapt_announcement_row(raw, source_id=feed_source_id, field_map=field_map)
        sid = adapted.get("source_id") or feed_source_id
        natural_ext = adapted.get("announcement_id") or deterministic_id(
            "ANNX", sid, adapted.get("model"), adapted.get("kind"),
            adapted.get("published_at_raw"), content_hash(raw), length=10)
        record_key = f"announcement:{sid}:{natural_ext}"
        _version, is_new = self.persist_raw(
            source_id=sid, record_key=record_key, payload=raw, run_id=run_id,
            fetched_at=fetched_at, source_updated_raw=adapted.get("published_at_raw"),
            adapter=adapter)
        provenance = {
            "source_id": sid, "feed_source_id": feed_source_id,
            "version_id": _version["version_id"], "payload_hash": _version["payload_hash"],
            "run_id": run_id, "fetched_at": fetched_at,
            "published_at": _version["source_updated_at"],
            "adapter": adapter, "adapter_version": ADAPTER_VERSION,
            "url": adapted.get("url"),
        }
        upsert_announcement(self.store, adapted, provenance=provenance,
                            external_id=adapted.get("announcement_id") or natural_ext)
        return is_new

    def ingest_announcement_rows(self, rows: list[dict[str, Any]], *, source_id: str,
                                 adapter: str = "generic",
                                 fetched_at: Any = None) -> dict[str, Any]:
        fetched_iso = iso(fetched_at or utc_now())
        run_id = self.store.new_id("RUN")
        new_versions = 0
        for raw in rows:
            new_versions += int(self._ingest_announcement_raw(
                raw, feed_source_id=source_id, run_id=run_id, fetched_at=fetched_iso,
                adapter=adapter, field_map=None))
        return {"new_versions": new_versions}

    # ------------------------------------------------------------------
    # 资产 / 设备事实
    # ------------------------------------------------------------------
    def load_asset_rows(self, rows: list[dict[str, Any]]) -> int:
        n = 0
        for row in rows:
            asset_id = (row.get("asset") or row.get("asset_id") or "").strip()
            if not asset_id:
                continue
            existing = self.store.assets.get(asset_id, {})
            model = (row.get("model") or existing.get("model") or "").strip() or None
            load = row.get("load") if row.get("load") not in (None, "") else (
                (existing.get("raw") or {}).get("load"))
            equipment_model = (row.get("equipment_model") or row.get("device_model")
                               or existing.get("equipment_model"))
            spec = build_spec(
                model=model,
                manufacturer=row.get("manufacturer") or (existing.get("spec") or {}).get("manufacturer"),
                load=load,
                dimensions={k: v for k, v in row.items()
                            if k in ("bore_mm", "outer_diameter_mm", "width_mm", "bore", "od", "width")},
                equipment_model=equipment_model,
                category=row.get("category"))
            if existing.get("spec") and not (spec.get("params") or spec.get("dimensions")):
                spec = existing["spec"]
            asset = {
                "id": asset_id,
                "asset": asset_id,
                "model": model,
                "equipment_model": (str(equipment_model).strip() if equipment_model else None),
                "load": load or existing.get("load"),
                "serial": row.get("serial") or existing.get("serial"),
                "spec": spec,
                "raw": {**(existing.get("raw") or {}), **{k: v for k, v in row.items() if v not in (None, "")}},
            }
            self.store.assets[asset_id] = asset
            if asset["model"]:
                ensure_material(self.store, spec=spec)
            n += 1
        # 资产入库后，所有映射参考证据可能变化
        for lkey in list(self.store.listings.keys()):
            refresh_listing_mapping(self.store, lkey)
        return n

    # ------------------------------------------------------------------
    # 订单 / 需求
    # ------------------------------------------------------------------
    def load_order_rows(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        created = updated = 0
        for row in rows:
            oid = (row.get("order_id") or row.get("id") or "").strip()
            if not oid:
                continue
            header = {
                "order_id": oid,
                "asset": (row.get("asset") or "").strip() or None,
                "item": (row.get("item") or "").strip() or None,
                "source": (row.get("source") or row.get("dealer") or "").strip() or None,
                "due": (row.get("due") or "").strip() or None,
                "qty": int(row["qty"]) if row.get("qty") not in (None, "") else 1,
                "state": (row.get("state") or "draft").strip() or "draft",
            }
            existing = self.store.orders.get(oid)
            if existing is None:
                self.store.orders[oid] = {
                    **header, "snapshot": None, "original_decision": None,
                    "last_check": None, "current_risks": [],
                    "header_history": [], "imported_at": iso(utc_now()),
                }
                created += 1
            else:
                # 已下单订单的关键字段受保护：记录差异而非悄然改写
                history = existing.setdefault("header_history", [])
                protected = existing.get("state") in ORDER_PLACED_STATES or existing.get("snapshot")
                for k, v in header.items():
                    if k == "order_id":
                        continue
                    if protected and k in ("item", "due", "source", "qty", "asset", "state"):
                        if existing.get(k) != v:
                            history.append({"at": iso(utc_now()), "field": k,
                                           "from": existing.get(k), "to": v,
                                           "note": "已确认订单受保护，导入变更未直接应用"})
                        continue
                    if existing.get(k) != v:
                        history.append({"at": iso(utc_now()), "field": k,
                                       "from": existing.get(k), "to": v})
                        existing[k] = v
                updated += 1
        return {"created": created, "updated": updated}
