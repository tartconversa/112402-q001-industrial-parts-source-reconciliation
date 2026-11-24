"""状态枚举、原因码与中英文案。

原因码是稳定的机器可读标识；``REASON_LABELS`` 提供采购员/治理人员可读的
中文说明。测试既可以断言原因码，也可以断言中文案中的关键词。
"""
from __future__ import annotations

# ---- 物料映射 / 治理状态 ---------------------------------------------------
CONFIRMED = "confirmed"          # 已确认同一物料
PENDING = "pending"              # 待核（信息不足 / 参数冲突 / 仅相似）
REJECTED = "rejected"            # 已排除（明确不是同一物料）

MAPPING_STATUSES = (CONFIRMED, PENDING, REJECTED)

# ---- 订单状态 --------------------------------------------------------------
DRAFT = "draft"                  # 未下单需求
CONFIRMED_ORDER = "confirmed"    # 已确认 / 已下单
ORDER_OPEN_STATES = ("draft", "open", "pending", "quoted")
ORDER_PLACED_STATES = ("confirmed", "ordered", "placed", "released", "po_issued")

# ---- 放行结论 --------------------------------------------------------------
RELEASABLE = "releasable"                 # 可直接放行采购
PENDING_VERIFICATION = "pending"          # 待核，不能放行
BLOCKED = "blocked"                       # 硬性阻断
REVIEW_REQUIRED = "review_required"       # 已下单：需复核（不改变原决策）

# ---- 公告状态/类型 ---------------------------------------------------------
ANN_ACTIVE = "active"
ANN_WITHDRAWN = "withdrawn"
ANN_SUPERSEDED = "superseded"

KIND_DISCONTINUATION = "discontinuation"  # 停产/EOL
KIND_REPLACEMENT = "replacement"          # 兼容替代信息
KIND_LEAD_TIME = "lead_time"              # 交期/交付变更
KIND_COMPATIBILITY = "compatibility"      # 兼容性公告

ANNOUNCEMENT_KINDS = {
    "discontinuation", "eol", "discontinued", "stop_sale", "停产",
    "replacement", "cross_reference", "supersession", "替代", "兼容替代",
    "lead_time", "delivery", "leadtime", "交期", "交付",
    "compatibility", "compat", "兼容",
}

# ---- 复核事项 --------------------------------------------------------------
REVIEW_OPEN = "open"
REVIEW_RESOLVED = "resolved"
SEV_BLOCKER = "blocker"
SEV_WARNING = "warning"
SEV_INFO = "info"

# ---- 采集来源类型 ----------------------------------------------------------
SRC_CATALOG = "catalog"
SRC_ANNOUNCEMENT = "announcement"
SRC_QUOTE_IMPORT = "quote_import"

# ---- 原因码 -> 中文案 + 建议后续处置 ---------------------------------------
# 每条：(中文说明模板, 默认后续处置)。模板可使用 {name}/{value}/{other} 等占位。
REASON_INFO: dict[str, tuple[str, str]] = {
    # 编码 / 相似性
    "manufacturer_code_mismatch": (
        "厂商编码不一致，不能认定为同一物料",
        "由物料治理人员核对厂商编码与铭牌后确认映射，不得按名称相似合并。"),
    "model_case_mismatch": (
        "厂商型号仅大小写不同；型号区分大小写，需人工确认",
        "向厂商目录核对准确大小写与完整型号后再确认。"),
    "similarity_not_evidence": (
        "商品名/型号仅表面相似，相似性不能作为同一物料或采购许可的依据",
        "依据厂商编码、尺寸与额定参数重新核验，必要时发起待核。"),
    "unknown_manufacturer_model": (
        "厂商编码未在权威目录/公告中出现",
        "补充厂商官方目录或公告证据后再核验。"),
    "ambiguous_multiple_matches": (
        "该经销商编码对应多个候选物料，存在歧义",
        "按来源批次、规格参数消歧并锁定唯一物料。"),
    # 参数 / 尺寸
    "parameter_conflict": (
        "额定参数 {name} 互相冲突：{value} ≠ {other}，不得视为可互换",
        "冻结该候选，转物料治理人员裁定；或选择参数匹配/已批准替代的来源。"),
    "dimension_conflict": (
        "尺寸 {name} 互相冲突：{value} ≠ {other}",
        "核对图纸/实测尺寸，冲突未消除前不得互换。"),
    "missing_attribute": (
        "缺少可比对的{kind} {name}，信息不足",
        "向经销商/厂商索取该参数后补录并重新核验。"),
    "missing_parameter": (
        "缺少额定参数 {name}，无法完成等效性确认",
        "索取额定参数；在补齐并核验前保持待核。"),
    "missing_dimension": (
        "缺少尺寸 {name}，无法确认装配互换性",
        "索取外形/安装尺寸后核验。"),
    "incomparable_attribute": (
        "参数 {name} 的量纲/等级无法自动比较",
        "由治理人员换算或判定等级后录入结论。"),
    "insufficient_identity_evidence": (
        "除厂商编码外缺少相互印证的尺寸/额定参数，证据不足",
        "至少取得一项匹配的尺寸或额定参数，或由治理人员凭权威资料确认。"),
    "unacknowledged_conflict": (
        "确认映射后出现尚未知悉的参数冲突",
        "重新复核冲突证据；如仍确认，需在确认时明确知悉并豁免该冲突。"),
    # 库存 / 交期
    "out_of_stock": (
        "该来源当前可用量为 0",
        "寻找同物料其他经销商现货，或走已批准替代/补货流程。"),
    "insufficient_quantity": (
        "可用量 {available} 小于需求数量 {required}",
        "拆分订单、补货或改用其他来源。"),
    "availability_unknown": (
        "来源未提供可用量",
        "向经销商确认库存后再承诺交期。"),
    "lead_time_exceeds_due": (
        "预计到货 {arrival} 晚于需求日期 {due}（交期 {lead_days} 天）",
        "催交/空运、更换现货来源，或与需求部门确认调整交付日期。"),
    # 停产 / 公告
    "discontinued_effective": (
        "厂商已于 {effective_date} 起停产该型号（公告 {announcement_id}）",
        "停止新采购；按公告选择经批准的替代件，或消化最后库存。"),
    "discontinuation_upcoming": (
        "厂商计划于 {effective_date} 停产，最后下单日 {last_order_date}",
        "评估末次采购量或切换替代，并在截止日前行动。"),
    "announcement_withdrawn": (
        "相关公告 {announcement_id} 已被厂商撤回",
        "该风险结论已失效，系统已重算；留存撤回记录。"),
    # 替代审批
    "substitution_not_approved": (
        "存在相似/公告推荐型号，但没有有效的替代审批",
        "由物料治理人员就设备型号与参数范围发起替代审批。"),
    "substitution_equipment_not_approved": (
        "替代审批 {approval_id} 未覆盖设备型号 {equipment_model}",
        "扩展审批的设备型号范围或寻找已覆盖本设备的替代。"),
    "substitution_parameter_out_of_range": (
        "需求参数 {name}={value} 超出审批 {approval_id} 的批准范围 {range}",
        "不得释放；申请扩大参数范围或选择合规替代。"),
    "substitution_approval_inactive": (
        "替代审批 {approval_id} 当前无效（过期/撤销）",
        "重新报批或改用有效审批的替代来源。"),
    "substitution_scope_incomplete": (
        "替代审批必须同时明确设备型号范围与参数范围",
        "补全审批的设备型号与参数范围，二者均批准后方可释放。"),
    # 已确认订单风险
    "delivery_date_superseded": (
        "新公告 {announcement_id} 改变了交期/停产安排，可能覆盖订单要求的交付日期 {due}",
        "采购员凭所附证据复核：催交、改期或切换已批准替代；原订单不被系统自动改写。"),
    "mapping_corrected": (
        "物料映射已被治理更正，原采购选择的物料归属发生变化",
        "核对实物与新映射；已下单决策保留，但需评估参数/供货风险。"),
    "approval_withdrawn": (
        "该订单依赖的替代审批 {approval_id} 已撤回",
        "原决策留痕；立即评估在途/到货与重新报批。"),
    "new_conflict_after_confirmation": (
        "确认后出现新的参数冲突证据",
        "复核新证据，必要时通知供应商或变更订单。"),
    "source_unavailable": (
        "来源 {source_id} 短时不可用，采集位置已保留",
        "稍后从检查点续采，无需重头抓取。"),
}


def reason_text(code: str, **fmt: object) -> str:
    """返回原因码对应的中文说明；未知码直接返回码本身。"""
    template = REASON_INFO.get(code, (code, ""))[0]
    try:
        return template.format(**fmt)
    except (KeyError, IndexError):
        return template


def suggested_action(code: str) -> str:
    return REASON_INFO.get(code, ("", "按待核流程处理。"))[1]


# 负载等级（可比较的有序类别）。数值越大额定越高。
LOAD_LEVELS: dict[str, int] = {
    "very_low": 1, "low": 2, "light": 2, "轻载": 2, "低": 2,
    "medium": 3, "normal": 3, "standard": 3, "中载": 3, "中": 3, "普通": 3,
    "high": 4, "heavy": 5, "高": 4, "重载": 5, "重": 5,
    "extra_high": 6, "extra_heavy": 6, "xhigh": 6, "特高": 6, "超重载": 6,
}

# 状态中文标签
STATUS_LABELS = {
    CONFIRMED: "已确认",
    PENDING: "待核",
    REJECTED: "已排除",
    RELEASABLE: "可放行",
    BLOCKED: "已阻断",
    REVIEW_REQUIRED: "需复核",
    REVIEW_OPEN: "待处理",
    REVIEW_RESOLVED: "已解决",
    SEV_BLOCKER: "阻断",
    SEV_WARNING: "警告",
    SEV_INFO: "提示",
}
