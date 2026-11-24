# 采购采集核验报告

- 核对基准日（as_of）：**2026-10-20**
- 物料 3（待核 3）；草稿需求 2（可放行 1 / 阻断 1 / 待核 0）；已确认 1
- 待处理复核事项 **7**（阻断级 3）

## 一、采购员待办（复核事项）

### [blocker] REV_642f2227b90b · 采购前核对未通过
- 对象：order `PO-32`；类型码：`parameter_conflict`
- 说明：额定参数 load 互相冲突：medium ≠ high，不得视为可互换
- 后续处置：冻结该候选，转物料治理人员裁定；或选择参数匹配/已批准替代的来源。

### [blocker] REV_023a8aaccf83 · 已确认订单风险 / 复核
- 对象：order `PO-31`；类型码：`delivery_date_superseded`
- 说明：新公告 A-LEAD-BR620 改变了交期/停产安排，可能覆盖订单要求的交付日期 2026-11-02
- 后续处置：采购员凭所附证据复核：催交、改期或切换已批准替代；原订单不被系统自动改写。
- 证据：
  - announcement source=MFR-WEB version=RV_38d7a6d1c7 fetched_at=2026-10-20T04:00:00+00:00 hash=397917b44f
  - source_record source=S1 version=RV_54cb43bafa fetched_at=2026-10-20T04:00:00+00:00 hash=075ed7b02d

### [blocker] REV_4733130ee60a · 采购前核对未通过
- 对象：order `PO-32`；类型码：`lead_time_exceeds_due`
- 说明：预计到货 2026-12-04 晚于需求日期 2026-11-08（交期 45 天）
- 后续处置：催交/空运、更换现货来源，或与需求部门确认调整交付日期。

### [warning] REV_167c24b4e85c · 物料映射待核
- 对象：material `MAT-BR620`；类型码：`parameter_conflict`
- 说明：额定参数 load 互相冲突：medium ≠ high，不得视为可互换
- 后续处置：None
- 证据：
  - catalog source=S2 version=RV_34cc63b2cb fetched_at=2026-10-20T04:00:00+00:00 hash=441a853546

### [warning] REV_a308428f5441 · 物料映射待核
- 对象：material `MAT-CT40`；类型码：`insufficient_identity_evidence`
- 说明：除厂商编码外缺少相互印证的尺寸/额定参数，证据不足
- 后续处置：None
- 证据：
  - catalog source=S1 version=RV_817e702640 fetched_at=2026-10-20T04:00:00+00:00 hash=356c5a07de

### [warning] REV_be1caed38b21 · 物料映射待核
- 对象：material `MAT-CT45`；类型码：`missing_attribute`
- 说明：缺少可比对的参数 control_channels，信息不足
- 后续处置：None
- 证据：
  - catalog source=S4 version=RV_14f9364b06 fetched_at=2026-10-20T04:00:00+00:00 hash=e9a6bf41f5

### [warning] REV_a5b803598716 · 物料映射待核
- 对象：material `MAT-CT45`；类型码：`insufficient_identity_evidence`
- 说明：除厂商编码外缺少相互印证的尺寸/额定参数，证据不足
- 后续处置：None
- 证据：
  - catalog source=S4 version=RV_14f9364b06 fetched_at=2026-10-20T04:00:00+00:00 hash=e9a6bf41f5

## 二、按订单核对

### PO-31 · confirmed · 需求日 2026-11-02
- 设备 `M1`，指定编码 `I-11`
- 原始决策（保留不改写）：releasable / direct / 2026-11-24T21:55:28+00:00
- 当前新风险：
  - [blocker] `delivery_date_superseded`（新公告）：新公告 A-LEAD-BR620 改变了交期/停产安排，可能覆盖订单要求的交付日期 2026-11-02
- 当前核对结论：**blocked**，可放行=False，放行方式=None
  - 阻断 `lead_time_exceeds_due`：预计到货 2026-12-04 晚于需求日期 2026-11-02（交期 45 天）

### PO-32 · draft · 需求日 2026-11-08
- 设备 `M2`，指定编码 `X-8`
- 当前核对结论：**blocked**，可放行=False，放行方式=None
  - 阻断 `parameter_conflict`：额定参数 load 互相冲突：medium ≠ high，不得视为可互换
  - 阻断 `lead_time_exceeds_due`：预计到货 2026-12-04 晚于需求日期 2026-11-08（交期 45 天）

### PO-33 · draft · 需求日 2026-11-09
- 设备 `M3`，指定编码 `I-12`
- 当前核对结论：**releasable**，可放行=True，放行方式=approved_substitution
  - 建议来源（approved_substitution）：批准替代 AP-CT40-CT45 -> S4/CT45-N6

## 三、按物料：来源差异与待核原因

### MAT-BR620 · BR-620 · 治理状态：待核
- 来源差异：
  - `load` ⚠冲突：S1/I-11=high, S2/X-8=medium, S3/BD-620H=high
  - `available`：S1/I-11=18, S2/X-8=6, S3/BD-620H=40
  - `lead_days`：S1/I-11=7, S3/BD-620H=5
  - `price`：S1/I-11=55.0, S3/BD-620H=58.5
- 待核原因：
  - `parameter_conflict`：额定参数 load 互相冲突：medium ≠ high，不得视为可互换（S2/X-8）
- 后续处置：
  - 冻结该候选，转物料治理人员裁定；或选择参数匹配/已批准替代的来源。
  - None

### MAT-CT40 · CT-40 · 治理状态：待核
- 待核原因：
  - `insufficient_identity_evidence`：除厂商编码外缺少相互印证的尺寸/额定参数，证据不足（S1/I-12）
- 后续处置：
  - 至少取得一项匹配的尺寸或额定参数，或由治理人员凭权威资料确认。
  - None

### MAT-CT45 · CT-45 · 治理状态：待核
- 待核原因：
  - `missing_attribute`：缺少可比对的参数 control_channels，信息不足（S4/CT45-N6）
  - `insufficient_identity_evidence`：除厂商编码外缺少相互印证的尺寸/额定参数，证据不足（S4/CT45-N6）
- 后续处置：
  - 向经销商/厂商索取该参数后补录并重新核验。
  - 至少取得一项匹配的尺寸或额定参数，或由治理人员凭权威资料确认。
  - None

## 四、采集来源与检查点

- `S1` status=success offset=3 done=True 位置=行 3/3 last_error=None
- `S2` status=success offset=3 done=True 位置=行 3/3 last_error=None
- `S3S4-FEED` status=success offset=2 done=True 位置=行 2/2 last_error=None
- `Q-WEEKLY` status=success offset=3 done=True 位置=行 3/3 last_error=None
- `MFR-ANN` status=success offset=2 done=True 位置=行 2/2 last_error=None
- `MFR-WEB` status=success offset=1 done=True 位置=行 1/1 last_error=None
