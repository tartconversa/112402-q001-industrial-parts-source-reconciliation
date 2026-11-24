"""ProcurementApp：对外统一门面（Facade）。

典型用法
--------
>>> from procurement import ProcurementApp
>>> app = ProcurementApp(as_of="2026-10-20")
>>> app.ingest_samples("samples")
>>> app.check_order("PO-32")["verdict"]
'blocked'

所有变更类方法都会：重算未下单需求 -> 对账复核事项 -> 原子落盘，
并返回受影响订单/新建复核事项等“影响面”信息。已确认订单的快照永不被改写。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from . import decisions as D
from . import governance as G
from . import reviews as R
from .announcements import withdraw_announcement as _withdraw_announcement
from .common import iso, parse_date, utc_now
from .ingest import IngestionService
from .parsers import parse_file
from .sources import FileTransport, collect_due, run_source
from .status import CONFIRMED, REJECTED


class ProcurementApp:
    def __init__(self, state_path: str | Path | None = None, *, as_of: str | None = None,
                 auto_refresh: bool = True, transport=None):
        from .store import Store
        self.store = Store(state_path)
        self.ingest = IngestionService(self.store)
        self.auto_refresh = auto_refresh
        self.transport = transport or FileTransport()
        self.fixed_as_of = parse_date(as_of)

    # ------------------------------------------------------------------
    # 时间 / 持久化
    # ------------------------------------------------------------------
    def as_of_date(self, as_of: str | None = None):
        return parse_date(as_of) or self.fixed_as_of or utc_now().date()

    def save(self) -> None:
        self.store.save()

    # ------------------------------------------------------------------
    # 来源注册与频率
    # ------------------------------------------------------------------
    def register_source(self, config: dict[str, Any]) -> dict[str, Any]:
        sid = config["id"]
        cfg = {
            "id": sid,
            "kind": config.get("kind", "catalog"),
            "adapter": config.get("adapter", "generic"),
            "field_map": config.get("field_map"),
            "frequency": config.get("frequency", "daily"),
            "frequency_seconds": config.get("frequency_seconds"),
            "path": config.get("path"),
            "url": config.get("url"),
            "batch_size": int(config.get("batch_size", 50)),
            "enabled": bool(config.get("enabled", True)),
            "note": config.get("note"),
        }
        self.store.sources[sid] = cfg
        self.store.audit("source.registered", source=sid, kind=cfg["kind"],
                         frequency=cfg["frequency"])
        self.save()
        return cfg

    def register_sources(self, configs: Iterable[dict[str, Any]] | str | Path) -> list[dict]:
        if isinstance(configs, (str, Path)):
            from .common import load_json
            data = load_json(configs)
            configs = data.get("sources", data) if isinstance(data, dict) else data
        return [self.register_source(c) for c in configs]

    def run_source(self, source_id: str, *, transport=None, now=None) -> dict[str, Any]:
        report = run_source(self.store, source_id, transport or self.transport,
                            self.ingest, now=now or self._now_dt())
        self.refresh(trigger=f"collect:{source_id}")
        return report

    def collect(self, *, force: bool = False, only: list[str] | None = None,
                transport=None, now=None) -> list[dict[str, Any]]:
        reports = collect_due(self.store, transport or self.transport, self.ingest,
                              now=now or self._now_dt(), force=force, only=only)
        self.refresh(trigger="collect_due")
        return reports

    def _now_dt(self):
        # 当设置了固定 as_of（仅日期），用该日期中午作为采集时刻，保证演示确定
        if self.fixed_as_of:
            from datetime import datetime, timezone, timedelta
            return datetime(self.fixed_as_of.year, self.fixed_as_of.month,
                            self.fixed_as_of.day, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        return utc_now()

    # ------------------------------------------------------------------
    # 文件 / 行数据导入
    # ------------------------------------------------------------------
    def _rows_from(self, source: Any) -> list[dict[str, Any]]:
        if isinstance(source, (str, Path)):
            return parse_file(source)
        return list(source)

    def ingest_catalog(self, source: Any, *, source_id: str = "catalog-feed",
                       adapter: str = "sample_catalog",
                       field_map: dict[str, str] | None = None,
                       fetched_at: Any = None, refresh: bool = True) -> dict[str, Any]:
        rows = self._rows_from(source)
        res = self.ingest.ingest_offer_rows(
            rows, source_id=source_id, kind="catalog", adapter=adapter,
            field_map=field_map, fetched_at=fetched_at or self._now_dt())
        if refresh:
            self.refresh(trigger="ingest_catalog")
        self.save()
        return res

    def import_quotes(self, source: Any, *, source_id: str = "quote-import",
                      adapter: str = "quote_standard",
                      field_map: dict[str, str] | None = None,
                      fetched_at: Any = None, refresh: bool = True) -> dict[str, Any]:
        rows = self._rows_from(source)
        res = self.ingest.ingest_offer_rows(
            rows, source_id=source_id, kind="quote_import", adapter=adapter,
            field_map=field_map, fetched_at=fetched_at or self._now_dt())
        if refresh:
            self.refresh(trigger="import_quotes")
        self.save()
        return res

    def ingest_announcements(self, source: Any, *, source_id: str = "mfr-announcements",
                             fetched_at: Any = None, refresh: bool = True) -> dict[str, Any]:
        rows = self._rows_from(source)
        res = self.ingest.ingest_announcement_rows(
            rows, source_id=source_id, fetched_at=fetched_at or self._now_dt())
        if refresh:
            self.refresh(trigger="ingest_announcements")
        self.save()
        return res

    def add_announcement(self, record: dict[str, Any], *, source_id: str = "mfr-announcements"
                         ) -> dict[str, Any]:
        self.ingest.ingest_announcement_rows([record], source_id=source_id,
                                             fetched_at=self._now_dt())
        impact = self.refresh(trigger="add_announcement")
        self.save()
        return {"announcement": record, "impact": impact}

    def import_assets(self, source: Any) -> int:
        rows = self._rows_from(source)
        n = self.ingest.load_asset_rows(rows)
        self.refresh(trigger="import_assets")
        self.save()
        return n

    def import_orders(self, source: Any) -> dict[str, int]:
        rows = self._rows_from(source)
        res = self.ingest.load_order_rows(rows)
        self.refresh(trigger="import_orders")
        self.save()
        return res

    def ingest_path(self, path: str | Path, *, kind: str = "auto",
                    source_id: str | None = None) -> dict[str, Any]:
        """导入单个文件或整个投放目录。

        ``kind='auto'`` 时按文件名关键词（announce/notice/eol/公告、quote/报价、
        order/订单、asset/设备、catalog/dealer/目录）推断类型；无法判定时按 catalog。
        """
        from .parsers import list_input_files
        files = list_input_files(path)
        results = []
        for fp in files:
            k = kind
            if k == "auto":
                name = fp.stem.lower()
                if any(w in name for w in ("announce", "notice", "eol", "gonggao", "公告")):
                    k = "announcements"
                elif any(w in name for w in ("quote", "quotation", "baojia", "报价")):
                    k = "quotes"
                elif any(w in name for w in ("order", "dingdan", "订单", "po")):
                    k = "orders"
                elif any(w in name for w in ("asset", "equipment", "shebei", "设备")):
                    k = "assets"
                else:
                    k = "catalog"
            if k == "announcements":
                r = self.ingest_announcements(fp, source_id=source_id or "mfr")
            elif k == "quotes":
                r = self.import_quotes(fp, source_id=source_id or "quote-import", refresh=False)
            elif k == "assets":
                r = {"count": self.import_assets(fp)}
            elif k == "orders":
                r = self.import_orders(fp)
            else:
                r = self.ingest_catalog(fp, source_id=source_id or "catalog-feed", refresh=False)
            results.append({"file": str(fp), "kind": k, "result": r})
        self.refresh(trigger="ingest_path")
        self.save()
        return {"files": len(results), "results": results}

    def ingest_samples(self, samples_dir: str | Path = "samples") -> dict[str, Any]:
        d = Path(samples_dir)
        catalog = self.ingest_catalog(d / "catalog.csv", refresh=False)
        assets = self.import_assets(d / "assets.csv")
        orders = self.import_orders(d / "orders.csv")
        impact = self.refresh(trigger="ingest_samples")
        return {"catalog": catalog, "assets": assets, "orders": orders, "impact": impact}

    # ------------------------------------------------------------------
    # 治理动作
    # ------------------------------------------------------------------
    def _resolve_lkey(self, item: str | None, source: str | None,
                      lkey: str | None) -> str:
        if lkey:
            if lkey not in self.store.listings:
                raise KeyError(lkey)
            return lkey
        if not item:
            raise ValueError("需要 item 或 lkey")
        if source:
            lk = f"{source}::{item}"
            if lk not in self.store.listings:
                raise KeyError(lk)
            return lk
        matches = [k for k, ln in self.store.listings.items() if ln.get("item") == item]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"找不到经销商编码: {item}")
        raise ValueError(f"编码 {item} 存在于多个来源 {matches}，请用 source 或 lkey 指定")

    def confirm_mapping(self, item: str | None = None, *, source: str | None = None,
                        lkey: str | None = None, material: str | None = None,
                        by: str = "governance", note: str = "",
                        acknowledge_conflicts: bool = False) -> dict[str, Any]:
        lk = self._resolve_lkey(item, source, lkey)
        mid = G.resolve_material_identifier(self.store, material)["id"] if material else None
        mapping = G.apply_manual_decision(
            self.store, lk, CONFIRMED, by=by, material_id=mid, note=note,
            acknowledge_conflicts=acknowledge_conflicts)
        impact = self.refresh(trigger="mapping_confirmed")
        self.save()
        return {"mapping": mapping, "impact": impact}

    def correct_mapping(self, item: str | None = None, *, to_material: str | None = None,
                        source: str | None = None, lkey: str | None = None,
                        by: str = "governance", note: str = "更正物料映射",
                        acknowledge_conflicts: bool = True) -> dict[str, Any]:
        if not to_material:
            raise ValueError("correct_mapping 需要 to_material")
        lk = self._resolve_lkey(item, source, lkey)
        mat = G.resolve_material_identifier(self.store, to_material)
        if mat is None:
            # 允许新型号
            mat = G.ensure_material(self.store, model=to_material)
        before = self.store.listings[lk].get("material_id")
        mapping = G.apply_manual_decision(
            self.store, lk, CONFIRMED, by=by, material_id=mat["id"], note=note,
            acknowledge_conflicts=acknowledge_conflicts, override_all_conflicts=True)
        self.store.audit("mapping.corrected", actor=by, listing=lk,
                         **{"from": before, "to": mat["id"]})
        impact = self.refresh(trigger="mapping_corrected")
        self.save()
        return {"mapping": mapping, "before_material": before,
                "after_material": mat["id"], "impact": impact}

    def reject_mapping(self, item: str | None = None, *, source: str | None = None,
                      lkey: str | None = None, by: str = "governance",
                      note: str = "") -> dict[str, Any]:
        lk = self._resolve_lkey(item, source, lkey)
        mapping = G.apply_manual_decision(self.store, lk, REJECTED, by=by, note=note)
        impact = self.refresh(trigger="mapping_rejected")
        self.save()
        return {"mapping": mapping, "impact": impact}

    def approve_substitution(self, **kwargs) -> dict[str, Any]:
        approval = G.add_approval(self.store, **kwargs)
        impact = self.refresh(trigger="approval_added")
        self.save()
        return {"approval": approval, "impact": impact}

    def withdraw_approval(self, approval_id: str, *, by: str = "governance",
                          reason: str = "") -> dict[str, Any]:
        ap = self.store.approvals[approval_id]
        ap["status"] = "revoked"
        ap["revoked_at"] = iso(self._now_dt())
        ap["revoke_reason"] = reason
        self.store.audit("approval.revoked", actor=by, approval=approval_id, reason=reason)
        impact = self.refresh(trigger="approval_revoked")
        self.save()
        return {"approval": ap, "impact": impact}

    def withdraw_announcement(self, announcement_id: str, *, reason: str = "",
                             by: str = "governance") -> dict[str, Any]:
        ann = _withdraw_announcement(self.store, announcement_id, reason=reason, by=by)
        impact = self.refresh(trigger="announcement_withdrawn")
        self.save()
        return {"announcement": ann, "impact": impact}

    # ------------------------------------------------------------------
    # 决策 / 订单
    # ------------------------------------------------------------------
    def check_order(self, order_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        chk = D.check_order(self.store, order_id, as_of_date=self.as_of_date(as_of))
        self.store.orders[order_id]["last_check"] = chk
        self.save()
        return chk

    # 常见别名，降低不同调用习惯的适配成本
    pre_purchase_check = check_order
    verify_order = check_order

    def confirm_order(self, order_id: str, *, by: str = "buyer", force: bool = False,
                      waiver: str = "", as_of: str | None = None) -> dict[str, Any]:
        res = D.confirm_order(self.store, order_id, by=by, force=force, waiver=waiver,
                              as_of_date=self.as_of_date(as_of), at=self._now_dt())
        self.refresh(trigger="order_confirmed")
        self.save()
        return res

    def apply_alternative(self, order_id: str, lkey: str, *, by: str = "buyer") -> dict[str, Any]:
        """采购员把【草稿】需求切换到另一个已确认同源物料来源；已确认订单不允许。"""
        from .status import ORDER_PLACED_STATES
        order = self.store.orders[order_id]
        if order.get("state") in ORDER_PLACED_STATES or order.get("snapshot"):
            raise PermissionError("已确认订单受保护，不能直接改写；请走复核/变更流程。")
        if lkey not in self.store.listings:
            raise KeyError(lkey)
        listing = self.store.listings[lkey]
        before = {"item": order.get("item"), "source": order.get("source")}
        order["item"] = listing.get("item")
        order["source"] = listing.get("source_id")
        self.store.audit("order.alternative_applied", actor=by, order=order_id,
                         **{"from": before, "to": {"item": order["item"], "source": order["source"]}})
        chk = self.check_order(order_id)
        self.refresh(trigger="alternative_applied")
        self.save()
        return {"order_id": order_id, "before": before,
                "after": {"item": order["item"], "source": order["source"]}, "check": chk}

    def recompute_open_requirements(self, trigger: str = "manual",
                                    as_of: str | None = None) -> dict[str, Any]:
        rec = D.recompute_open_requirements(
            self.store, trigger=trigger, as_of_date=self.as_of_date(as_of), now=self._now_dt())
        sync = R.sync_review_items(self.store, as_of_date=self.as_of_date(as_of),
                                   now=self._now_dt())
        self.save()
        return {"recomputation": rec, "review_sync": sync}

    recompute = recompute_open_requirements

    def sync_reviews(self, as_of: str | None = None) -> dict[str, Any]:
        res = R.sync_review_items(self.store, as_of_date=self.as_of_date(as_of),
                                  now=self._now_dt())
        self.save()
        return res

    def refresh(self, trigger: str = "refresh") -> dict[str, Any]:
        """重算所有映射、未下单需求并对账复核事项。"""
        G.reevaluate_all_mappings(self.store)
        D.ensure_imported_baseline(self.store, now=self._now_dt())
        as_of = self.as_of_date()
        rec = D.recompute_open_requirements(self.store, trigger=trigger, as_of_date=as_of,
                                           now=self._now_dt())
        sync = R.sync_review_items(self.store, as_of_date=as_of, now=self._now_dt())
        self.save()
        return {"recomputation": rec, "review_sync": sync}

    # ------------------------------------------------------------------
    # 视图 / 查询
    # ------------------------------------------------------------------
    def material_view(self, identifier: str) -> dict[str, Any]:
        from .views import material_view
        return material_view(self.store, identifier)

    def order_view(self, order_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        from .views import order_view
        return order_view(self.store, order_id, as_of_date=self.as_of_date(as_of))

    def dashboard(self) -> dict[str, Any]:
        from .views import dashboard
        return dashboard(self.store)

    def open_review_items(self, **filters) -> list[dict[str, Any]]:
        return R.open_reviews(self.store, **filters)

    def differences(self, identifier: str) -> list[dict[str, Any]]:
        return self.material_view(identifier)["source_differences"]

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------
    def write_reports(self, outdir: str | Path = "output") -> dict[str, Path]:
        from .reporting import write_reports
        return write_reports(self, outdir)

    def to_dict(self) -> dict[str, Any]:
        import json
        return json.loads(json.dumps(self.store.data, ensure_ascii=False, default=str))
