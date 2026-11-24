# 关键零部件供应变化的采集核验与替代确认

一套**可重复运行、仅依赖 Python 3 标准库**的采集应用，面向轴承（如 BR-620）与控制器
（如 CT-40）的多经销商供货、厂商停产/替代公告与定时报价导入。它把“采集 → 证据留存 →
物料治理 → 采购前核对 → 复核事项 → 更正后重算”串成闭环，并坚守一条安全底线：

> **名称/型号相似绝不是合并物料或采购放行的依据；参数冲突或信息不足一律保持“待核”；
> 替代件只有在“对应设备型号范围”和“参数范围”都获批准时才能释放。**

所有示例标识符与数值均为合成数据。CSV 为 UTF-8，首行表头，空单元格表示来源未提供值。
型号区分大小写。

---

## 1. 快速开始

```bash
# 环境自检：样本 CSV 列名与关联（仓库自带测试）
python3 -m unittest discover -s tests -v

# 运行内置完整演练（确定性基准日 2026-10-20），生成 JSON/Markdown 报告
python3 -m procurement demo --fresh
#   -> output/procurement_report.md   （给采购员/治理人员阅读）
#   -> output/procurement_report.json （完整结构化结果）
#   -> output/buyer_todo.json         （待处理复核事项）
#   -> output/demo_state.json         （可重复运行的状态库/证据链）

# 或从 samples/ + fixtures/ 跑通用流水线
python3 -m procurement --as-of 2026-10-20 run --fresh

# 常用查询
python3 -m procurement --state output/demo_state.json check PO-32
python3 -m procurement --state output/demo_state.json material BR-620
python3 -m procurement --state output/demo_state.json order PO-31
python3 -m procurement --state output/demo_state.json reviews
python3 -m procurement --state output/demo_state.json dashboard --json
```

CLI 全局参数 `--state/--as-of/--json` 放在子命令前后均可。`check` 在不可放行时退出码为 `2`，
便于脚本/CI 卡口。

### 30 秒 Python API

```python
from procurement import ProcurementApp

app = ProcurementApp("output/state.json", as_of="2026-10-20")
app.ingest_samples("samples")

app.check_order("PO-31")          # -> verdict='releasable', release_kind='direct'
app.check_order("PO-32")         # -> blocked: parameter_conflict load medium != high
app.check_order("PO-33")         # -> blocked: out_of_stock + 待核证据不足

app.material_view("BR-620")       # 各来源差异/待核原因/后续处置/证据
app.order_view("PO-31")           # 原始快照 + 当前风险 + 复核事项
app.open_review_items()           # 采购员待办
```

---

## 2. 样本数据所讲的故事

| 订单 | 设备需求 | 指定经销商编码 | 系统结论 |
|---|---|---|---|
| **PO-31**（confirmed） | M1 = BR-620 / high | S1 `I-11` = BR-620 / high / 库存 18 | 编码仅分隔符差异（BR-620≡BR620），负载一致 → **可直接放行** |
| **PO-32**（draft） | M2 = BR620 / high | S2 `X-8` = BR620 / **medium** / 库存 6 | 编码归一后相同但额定负载冲突 → **阻断 + 待核**，不得当成可互换 |
| **PO-33**（draft） | M3 = CT-40（无负载值） | S1 `I-12` = CT-40 / 参数缺失 / **库存 0** | **缺货阻断** + `insufficient_identity_evidence` 待核 |

`fixtures/` 进一步演示：宽表头经销商 `S3/S4`、定时报价、CT-40 停产公告与 CT-45 替代索引、
设备台账（`M3→PACKER-300`）、双范围替代审批，以及一份**晚到的 HTML 交期延长公告**
（BR-620 交期 45 天，导致已确认 PO-31 的 2026-11-02 需求日可能被覆盖 → 生成带证据的
`delivery_date_superseded` 复核事项，而订单原始决策与快照保持不变）。

---

## 3. 架构与模块

```
samples/                 题目给定的三份 CSV（勿改表头，测试依赖）
fixtures/                额外异构来源：宽表头 CSV / 报价 CSV / JSON 公告 / HTML 公告 / 审批 / 设备台账
procurement/
  common.py              时间解析(ISO/时区)、稳定哈希、原子 JSON 落盘、CSV 读取
  status.py              状态/原因码 + 中文说明 + 建议后续处置 + 负载等级
  specs.py               型号归一(分隔符不敏感/大小写敏感)、参数建模、保守比对、审批范围覆盖
  adapters.py            不同字段格式的表头候选 + field_map 预设 + 公告适配 + 自由文本补抽
  parsers.py             CSV / JSON / HTML(<table>) / EML(含 CSV/JSON 附件) 离线解析
  store.py               单一 JSON 状态库 + 审计日志 + 集合访问器
  ingest.py              原始版本化(raw versions)、证据链、Listing/Quote/公告/资产/订单入库与合并
  sources.py             来源频率、检查点 offset、FileTransport、可脚本化 ScriptedTransport、采集器
  governance.py          物料、映射核验、人工确认/更正/排除、替代审批与有效期
  announcements.py       公告入库、截至 as_of 的“当前生效”视图、撤回
  decisions.py           采购前核对、双范围替代判定、订单快照、已确认订单风险扫描、未下单重算
  reviews.py             复核事项幂等对账（新建/保留/自动解决）
  views.py               物料视图 / 订单视图 / 采购员看板
  reporting.py           JSON + Markdown 报告
  engine.py              ProcurementApp 门面（推荐入口）
  cli.py / __main__.py   命令行
  demo.py                确定性完整演练
tests/                   题目自带样本测试 + 41 条行为/CLI/边界测试
app.py main.py solution.py pipeline.py  常见入口名的兼容垫片
conftest.py              保证 pytest 下也能 import procurement
```

数据流：

```
来源文件/脚本化 Transport
   └─ sources.run_source（频率判断 + 每批检查点 + 故障保留 offset）
        └─ parsers/adapters（解析 + 字段格式归一）
             └─ ingest.IngestionService（raw payload 哈希/版本 + provenance）
                  ├─ governance（物料/映射：confirmed/pending/rejected + 原因）
                  ├─ announcements（生效/撤回） + approvals（设备×参数范围）
                  └─ decisions.check_order（供货 + 停产 + 交期 + 替代）
                       ├─ drafts → last_check + 重算记录
                       └─ confirmed → 不可变 snapshot + current_risks
                            └─ reviews.sync_review_items（带证据待办，幂等对账）
                                 └─ views / reporting（按物料、按订单）
```

---

## 4. 关键业务规则与稳定原因码

* **型号身份**：`model_key` 删除 `- _ 空格 / .` 等分隔符但**保留大小写**
  （`BR-620≡BR620`，而 `BR620` 与 `br620` 仅 `case_only`，转待核）。
* **自动确认条件（全部满足）**：编码关系为 `exact/normalized_code`；无参数/尺寸冲突；
  至少有一项尺寸或额定参数相互匹配。否则 `pending`，原因例如：
  `parameter_conflict`、`dimension_conflict`、`missing_attribute`、
  `insufficient_identity_evidence`、`model_case_mismatch`、`similarity_not_evidence`。
* **相似性**：始终计算 `similarity` 并展示，但不参与确认/放行。
* **数值/等级**：尺寸与力度量做单位归一（mm/cm/in、N/kN）；负载支持
  low/medium/high/heavy 及中文 轻载/中载/高/重载 的有序等级比较。
* **采购前核对**：映射/参数符合性、可用库存与需求量、按最保守（最长）交期推算的预计到货
  vs 需求日、当前生效停产公告。结论为 `releasable / pending / blocked`。
* **替代双范围**：`approval_is_active` 且
  `check_approval_scope().both_approved is True`：
  * 设备：订单上下文 token（`equipment_model`、`asset` 编号，必要时含部件型号）命中
    `equipment_models` 或 `asset_ids`；空范围 = 不批准；
  * 参数：`parameter_ranges` 非空，需求值与替代件实际值都必须落在范围内
    （支持 `{min,max}`、`[lo,hi]`、`{values:[...]}`、等级下限）。
    替代件缺少被范围约束的参数同样不放行。
* **公告时间**：按 `published/effective` 与基准日判断“当前生效”，撤回即失效；
  **报价与公告是不同事实类型，再旧的报价也不能抹掉生效停产公告**。

---

## 5. 可追溯性与可重复运行

* 每条 Listing/Quote/Announcement 的 `provenance` 含：
  `source_id / feed_source_id / version_id / payload_hash(SHA-256) / run_id /
  fetched_at / source_updated_at / adapter(_version) / url`。
* `store.data["record_versions"]` 保存**不可变原始载荷**；版本 ID 由
  `逻辑记录键 + 载荷哈希` 决定，重复采集、跨进程重启都不会产生重复版本。
* 状态用“临时文件 + `os.replace`”原子写入单个 JSON（默认
  `output/state.json`，demo 用 `output/demo_state.json`）；`state_path=None` 为纯内存模式，
  便于测试隔离。
* 每次治理/订单/公告动作写入 `audit` 审计事件。

### 来源频率与断点续采

```python
from procurement import ProcurementApp
from procurement.sources import ScriptedTransport

app = ProcurementApp(as_of="2026-10-20")
app.register_source({"id": "Sx", "kind": "catalog", "frequency": "daily", "batch_size": 2})
rows = [{"item": f"I-{i}", "model": "BR-620", "load": "high"} for i in range(5)]

# 第 2 批拉取时来源短时不可用：offset 停在 2
app.run_source("Sx", transport=ScriptedTransport({"Sx": rows}, fail_at={"Sx": 2}))
# 恢复后从 offset=2 续采剩余 3 条，最终恰好 5 个经销商编码、无重复
app.run_source("Sx", transport=ScriptedTransport({"Sx": rows}))

app.collect(force=False)   # 按各来源 frequency 决定是否 skipped_not_due / 运行
```

`FileTransport` 直接读 CSV/JSON/HTML/EML；生产中可实现自己的 Transport
（HTTP 抓取、邮箱拉取）并复用同一 `Sink.persist_batch` 与检查点协议。

---

## 6. 更正、撤回与“已确认订单不被悄然改写”

* 目录刷新只更新 Listing/版本与**当前风险**，绝不修改已确认订单头或其 `snapshot`
  （含 `checksum`、当时的 `fact_version_ids`、`announcement_ids_checked`、建议来源）。
* `correct_mapping(...)` / `withdraw_announcement(...)` / `approve_substitution(...)`
  后自动 `refresh()`：
  * 所有 `draft/open` 需求重新执行 `check_order`，并在 `recomputations` 留痕
    （before/after/原因签名/changed）；
  * 已确认订单保留 `original_decision/snapshot`，只刷新 `current_risks`
    （如 `mapping_corrected`、`approval_withdrawn`、`out_of_stock`、
    `delivery_date_superseded`）。
* `reviews.sync_review_items()` 用确定性 ID 幂等对账：新问题建 open 事项，
  重复运行不重建；更正/撤回使原因消失时，对应自动事项被置为 `resolved` 并写
  `resolution_note`，证据与历史保留。

```python
app.withdraw_announcement("A-EOL-CT40", reason="厂商更正编号")
app.correct_mapping(item="X-8", source="S2", to_material="MAT-BR620-MEDIUM",
                    note="实物为中负载变体")
```

---

## 7. 扩展一个新来源格式

在 `procurement/adapters.py` 的 `HEADER_CANDIDATES` 增加表头别名，或在注册来源时传
`field_map={"item": "你的编码列", "model": "你的型号列", ...}` 与
`adapter="dealer_wide"` 等预设；规格列（孔径/外径/宽度/电压/…）会自动进入 `build_spec`
并参与核验。新接入 HTTP 时实现一个含 `pull_batch(source_cfg, offset, batch_size)` 的
Transport 即可获得频率、检查点、重试位置与审计能力。

---

## 8. 测试

```bash
python3 -m unittest discover -s tests -v
```

包含：样本列名/关联自检，以及覆盖型号归一、参数/尺寸冲突、信息不足、双范围替代、
停产/交期、旧报价不覆盖停产、证据链、断点续采、频率门控、跨重启幂等、快照不可变、
更正/撤回重算、复核事项对账、视图与 CLI、HTML/EML 解析的行为测试。
