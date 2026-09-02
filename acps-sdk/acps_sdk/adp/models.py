"""
ADP (Agent Discovery Protocol) 数据模型定义

基于 ACPs-spec-ADP 协议规范定义的数据结构。
使用 Pydantic V2 实现类型验证和序列化。

注意：
- 请求体和响应体中的字段使用 lowerCamelCase 小驼峰命名法（通过 alias 实现）。
- Python 属性名使用 snake_case。
- 所有时间戳均使用 ISO 8601 格式，并必须包含时区信息。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .constants import (
    FILTER_LOGIC_AND,
    FORWARD_DEPTH_LIMIT_DEFAULT,
    FORWARD_DEPTH_LIMIT_MAX,
    FORWARD_DEPTH_LIMIT_MIN,
    FORWARD_EACH_TIMEOUT_MS_DEFAULT,
    FORWARD_FANOUT_LIMIT_DEFAULT,
    FORWARD_FANOUT_LIMIT_MAX,
    FORWARD_FANOUT_LIMIT_MIN,
    FORWARD_TOTAL_TIMEOUT_MS_DEFAULT,
    QUERY_TYPE_EXPLICIT,
    QUERY_TYPES,
)


# =============================================================================
# FilterOperator 过滤运算符
# =============================================================================


class FilterOperator(str, Enum):
    """
    ADP 过滤运算符枚举。

    通用：eq（等于）、ne（不等于）、exists（字段存在性）。
    比较：gt、gte、lt、lte（数值/日期/字符串字典序）、between（闭区间）。
    集合：in_（值在列表中）、nin（值不在列表中）。
    字符串：contains、notContains、startsWith、endsWith（默认大小写不敏感）。
    大小写敏感变体：加 Cs 后缀。
    数组：anyOf、allOf、noneOf、size 系列。
    Map/对象：hasKey、hasNoKey、hasAnyKey、hasAllKeys。
    """

    # ── 通用：等值与存在性 ──
    EQ = "eq"
    NE = "ne"
    EXISTS = "exists"

    # ── 比较：数值、日期、字符串字典序 ──
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"

    # ── 集合：值列表匹配 ──
    IN = "in"
    NIN = "nin"

    # ── 字符串：模式匹配（默认大小写不敏感） ──
    CONTAINS = "contains"
    NOT_CONTAINS = "notContains"
    STARTS_WITH = "startsWith"
    ENDS_WITH = "endsWith"

    # ── 字符串：大小写敏感变体（Cs = Case Sensitive） ──
    EQ_CS = "eqCs"
    NE_CS = "neCs"
    IN_CS = "inCs"
    NIN_CS = "ninCs"
    CONTAINS_CS = "containsCs"
    NOT_CONTAINS_CS = "notContainsCs"
    STARTS_WITH_CS = "startsWithCs"
    ENDS_WITH_CS = "endsWithCs"

    # ── 数组：集合运算 ──
    ANY_OF = "anyOf"
    ALL_OF = "allOf"
    NONE_OF = "noneOf"
    SIZE = "size"
    SIZE_GT = "sizeGt"
    SIZE_GTE = "sizeGte"
    SIZE_LT = "sizeLt"
    SIZE_LTE = "sizeLte"

    # ── Map/对象：键检查 ──
    HAS_KEY = "hasKey"
    HAS_NO_KEY = "hasNoKey"
    HAS_ANY_KEY = "hasAnyKey"
    HAS_ALL_KEYS = "hasAllKeys"


# =============================================================================
# FilterCondition 单个过滤条件
# =============================================================================


class FilterCondition(BaseModel):
    """
    单个过滤条件，描述对 ACS 某个字段施加的匹配规则。

    Attributes:
        field: 字段路径，使用点号分隔表示嵌套。对于数组字段，条件应用于
               每个元素，任意元素满足即匹配。
        op: 匹配运算符。
        value: 匹配值，类型取决于 field 的数据类型和 op 运算符。
    """

    field: str = Field(
        ...,
        description=(
            "字段路径，使用点号分隔表示嵌套。"
            "对于数组字段，条件应用于每个元素，任意元素满足即匹配。"
        ),
        examples=["active", "provider.countryCode", "skills.tags"],
    )

    op: FilterOperator = Field(
        ...,
        description="匹配运算符。",
        examples=["eq", "contains", "anyOf"],
    )

    value: Optional[Any] = Field(
        default=None,
        description=(
            "匹配值。类型取决于 field 的数据类型和 op 运算符。"
            "字符串运算符: string; 列表运算符(in/nin): 同类型值数组; "
            "数值/日期比较: number 或 ISO 8601 字符串; "
            "区间运算符(between): [lower, upper] 二元组; "
            "布尔运算符: boolean; 存在性运算符(exists): boolean; "
            "数组运算符(anyOf/allOf/noneOf): 期望匹配的值数组; "
            "数组大小运算符(size): number; "
            "Map 键运算符(hasKey/hasAnyKey/hasAllKeys): string 或 string[]"
        ),
        examples=[True, "北京", ["CN", "US"], 3],
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# DiscoveryFilter 过滤条件集合
# =============================================================================


class DiscoveryFilter(BaseModel):
    """
    过滤条件集合。

    采用通用条件数组模式，支持逻辑组合（AND/OR/NOT），可对 ACS 中所有可查询字段进行匹配。
    各条件之间的逻辑关系由 logic 字段控制（默认 AND）。
    支持通过 groups 嵌套子条件组，实现任意复杂的逻辑表达。
    建议嵌套不超过 3 层。
    """

    conditions: Optional[List[FilterCondition]] = Field(
        default=None,
        description="过滤条件列表。与 groups 中的子条件组按 logic 指定的逻辑关系组合。",
    )

    groups: Optional[List["DiscoveryFilter"]] = Field(
        default=None,
        description=(
            "嵌套的子条件组，每个子组可拥有独立的 logic。"
            "建议嵌套不超过 3 层，以保持可读性和转发性能。"
        ),
    )

    logic: Optional[Literal["and", "or", "not"]] = Field(
        default="and",
        description=(
            "本层条件和子条件组之间的逻辑关系。"
            "'and'（默认）：所有条件和子组均须满足；"
            "'or'：至少一个条件或子组满足即可；"
            "'not'：对本层整体结果取反。"
        ),
    )

    model_config = ConfigDict(populate_by_name=True)


# 解析前向引用
DiscoveryFilter.model_rebuild()


# =============================================================================
# DiscoveryContext 上下文载荷
# =============================================================================


class DiscoveryContext(BaseModel):
    """
    上下文载荷结构。

    旨在描述请求侧可选的会话与用户背景切片，供发现服务器理解意图。
    建议控制在约 2KB 以内，调用方应避免携带敏感信息。
    """

    conversation_id: Optional[str] = Field(
        default=None,
        alias="conversationId",
        description="客户端自定义的会话 ID，用于关联多轮查询。",
    )

    recent_turns: Optional[List[str]] = Field(
        default=None,
        alias="recentTurns",
        description=(
            "近期对话摘要或要点。" "建议按时间排序，避免携带完整原始对话以控制大小。"
        ),
        examples=[["上一轮询问了北京菜系", "用户偏好健康饮食"]],
    )

    user_profile: Optional[Dict[str, Any]] = Field(
        default=None,
        alias="userProfile",
        description="匿名化用户画像片段，如地理位置、预算偏好等。",
        examples=[{"city": "北京", "budget": "medium"}],
    )

    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="额外的上下文扩展，供上下游约定使用。",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# DiscoveryRequest 发现请求
# =============================================================================


class DiscoveryRequest(BaseModel):
    """
    ADP 发现请求数据结构。

    请求智能体通过此结构向发现服务器发起能力发现请求。
    """

    type: Optional[str] = Field(
        default=QUERY_TYPE_EXPLICIT,
        description=(
            "查询类型。explicit: 明确查询（默认），query 有明确意图并按 filter 过滤；"
            "exploratory: 探索性查询，用户没有明确目标；"
            "trending: 热门查询，返回当前流行的智能体；"
            "filtered: 过滤查询，只按 filter 过滤，query 被忽略。"
        ),
        examples=["explicit", "exploratory", "trending", "filtered"],
    )

    query: Optional[str] = Field(
        default=None,
        description=(
            "能力查询，自然语言描述的请求智能体所需的服务能力。"
            "type=explicit 时该字段必填。"
        ),
        examples=["我需要一个可以做北京美食推荐的智能体"],
    )

    context: Optional[DiscoveryContext] = Field(
        default=None,
        description=(
            "结构化上下文信息。"
            "可用于携带多轮对话摘要、用户画像切片或其它辅助意图理解的数据。"
            "推荐使用轻量 JSON 结构并控制在约 2KB 以内。"
        ),
    )

    limit: Optional[int] = Field(
        default=None,
        description=(
            "最大返回数量。"
            "指定返回的候选服务智能体的最大数量，未指定则使用服务器默认值。"
        ),
        ge=1,
        examples=[5, 10],
    )

    filter: Optional[DiscoveryFilter] = Field(
        default=None,
        description=(
            "结构化过滤条件。"
            "采用通用条件数组模式对 ACS 字段进行匹配，支持丰富的运算符和逻辑组合。"
        ),
    )

    forward_depth_limit: Optional[int] = Field(
        default=None,
        alias="forwardDepthLimit",
        description=(
            "单条链路允许的最大转发深度。"
            f"服务器默认 {FORWARD_DEPTH_LIMIT_DEFAULT}，"
            f"绝对上限 {FORWARD_DEPTH_LIMIT_MAX}。"
        ),
        ge=FORWARD_DEPTH_LIMIT_MIN,
        le=FORWARD_DEPTH_LIMIT_MAX,
    )

    forward_fanout_limit: Optional[int] = Field(
        default=None,
        alias="forwardFanoutLimit",
        description=(
            "允许的最大并发下游请求数量。"
            f"服务器默认 {FORWARD_FANOUT_LIMIT_DEFAULT}，"
            f"绝对上限 {FORWARD_FANOUT_LIMIT_MAX}。"
        ),
        ge=FORWARD_FANOUT_LIMIT_MIN,
        le=FORWARD_FANOUT_LIMIT_MAX,
    )

    forward_fanout_remaining: Optional[int] = Field(
        default=None,
        alias="forwardFanoutRemaining",
        description=(
            "当前分支尚可消耗的 fan-out 额度。"
            "原始请求通常不设置，由第一跳发现服务器根据 forwardFanoutLimit 初始化。"
        ),
        ge=0,
    )

    forward_chain: Optional[List[str]] = Field(
        default=None,
        alias="forwardChain",
        description=(
            "转发追踪链。每个字符串为一个智能体身份码（AIC）。"
            "原始请求无需携带该字段，由每一跳的发现服务器依次维护。"
        ),
    )

    forward_trusted_servers: Optional[List[str]] = Field(
        default=None,
        alias="forwardTrustedServers",
        description=(
            "信任的发现服务器 AIC 列表。"
            "由首个接收请求的发现服务器根据自身信任配置填充。"
            "后续节点在转发时应仅向列表中的 AIC 对应的发现服务器转发。"
        ),
    )

    forward_signatures: Optional[List[str]] = Field(
        default=None,
        alias="forwardSignatures",
        description=(
            "转发发现服务器的数字签名。"
            "由每一跳的发现服务器在转发前生成并附加。"
            "顺序与 forwardChain 保持一致。"
        ),
    )

    forward_each_timeout_ms: Optional[int] = Field(
        default=None,
        alias="forwardEachTimeoutMs",
        description=(
            f"每次转发的请求超时，单位毫秒。缺省可为 {FORWARD_EACH_TIMEOUT_MS_DEFAULT}ms。"
            "用于控制单跳的最大等待时间，防止单个节点阻塞整体响应。"
        ),
        gt=0,
    )

    forward_total_timeout_ms: Optional[int] = Field(
        default=None,
        alias="forwardTotalTimeoutMs",
        description=(
            f"转发请求的总超时，单位毫秒。缺省可为 {FORWARD_TOTAL_TIMEOUT_MS_DEFAULT}ms。"
            "用于控制整个转发链的最大等待时间。"
            "此值需要根据剩余时间对下一跳做动态调整。"
        ),
        gt=0,
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: Optional[str]) -> Optional[str]:
        """校验查询类型是否合法。"""
        if v is not None and v not in QUERY_TYPES:
            raise ValueError(f"无效的查询类型: {v!r}，允许的类型: {QUERY_TYPES}")
        return v

    @model_validator(mode="after")
    def validate_explicit_query(self) -> "DiscoveryRequest":
        """当 type=explicit 时，query 不能为空。"""
        if self.type == QUERY_TYPE_EXPLICIT:
            if not self.query or not self.query.strip():
                raise ValueError("type='explicit' 时 query 字段必填且不能为空字符串")
        return self

    def get_effective_depth_limit(self) -> int:
        """获取有效的转发深度限制（若未指定则返回默认值）。"""
        return self.forward_depth_limit or FORWARD_DEPTH_LIMIT_DEFAULT

    def get_effective_fanout_limit(self) -> int:
        """获取有效的转发扇出限制（若未指定则返回默认值）。"""
        return self.forward_fanout_limit or FORWARD_FANOUT_LIMIT_DEFAULT

    def get_effective_fanout_remaining(self) -> int:
        """获取有效的剩余扇出额度（若未指定则使用 fanout limit）。"""
        if self.forward_fanout_remaining is not None:
            return self.forward_fanout_remaining
        return self.get_effective_fanout_limit()

    def get_effective_each_timeout_ms(self) -> int:
        """获取有效的单跳超时（若未指定则返回默认值）。"""
        return self.forward_each_timeout_ms or FORWARD_EACH_TIMEOUT_MS_DEFAULT

    def get_effective_total_timeout_ms(self) -> int:
        """获取有效的总超时（若未指定则返回默认值）。"""
        return self.forward_total_timeout_ms or FORWARD_TOTAL_TIMEOUT_MS_DEFAULT

    def get_forward_chain_length(self) -> int:
        """获取当前转发链长度。"""
        return len(self.forward_chain) if self.forward_chain else 0

    def to_json(self, **kwargs) -> str:
        """序列化为 JSON 字符串（使用 camelCase alias）。"""
        return self.model_dump_json(by_alias=True, exclude_none=True, **kwargs)

    def to_dict(self, **kwargs) -> Dict[str, Any]:
        """序列化为字典（使用 camelCase alias）。"""
        return self.model_dump(by_alias=True, exclude_none=True, **kwargs)

    @classmethod
    def from_json(cls, json_str: str) -> "DiscoveryRequest":
        """从 JSON 字符串反序列化。"""
        return cls.model_validate_json(json_str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveryRequest":
        """从字典反序列化。"""
        return cls.model_validate(data)


# =============================================================================
# DiscoveryAgentSkill 单个智能体技能匹配结果
# =============================================================================


class DiscoveryAgentSkill(BaseModel):
    """
    单个智能体技能的匹配结果。
    """

    aic: str = Field(
        ...,
        description=(
            "智能体身份码（AIC）。"
            "用于关联 DiscoveryResult.acsMap 中的完整 ACS 信息。"
        ),
    )

    skill_id: str = Field(
        ...,
        alias="skillId",
        description="在 ACS 中匹配的 Skill ID。",
    )

    ranking: int = Field(
        ...,
        description=("匹配排名。" "排名顺序是数字的自然顺序，数字值越大排名越靠后。"),
    )

    memo: Optional[str] = Field(
        default=None,
        description="备注信息。",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# DiscoveryAgentGroup 按分组组织的智能体匹配结果
# =============================================================================


class DiscoveryAgentGroup(BaseModel):
    """
    按分组组织的智能体匹配结果。

    发现服务器可将查询拆解为多个子任务或类别，每个分组对应一个维度。
    若无需分组，使用单条默认分组（如 group 为原始查询文本或空字符串）。
    """

    group: str = Field(
        ...,
        description=(
            "分组标识。可以是子任务描述、类别名称等，用于标识该组结果的来源或维度。"
            "若无分组需求，可设为原始查询文本或空字符串。"
        ),
    )

    agent_skills: List[DiscoveryAgentSkill] = Field(
        default_factory=list,
        alias="agentSkills",
        description="该分组下匹配到的智能体技能列表。",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# DiscoveryRoute 发现结果的路由级细分
# =============================================================================


class DiscoveryRoute(BaseModel):
    """
    发现结果的路由级细分。

    每条路由对应一次链式或聚合转发路径及其候选结果。
    """

    forward_chain: List[str] = Field(
        ...,
        alias="forwardChain",
        description=(
            "转发链路所经过的发现服务器 AIC 列表。"
            "顺序从第一跳（接收到原始请求的发现服务器）到最终返回结果的发现服务器。"
        ),
    )

    agent_groups: List[DiscoveryAgentGroup] = Field(
        default_factory=list,
        alias="agentGroups",
        description=(
            "此链路返回的按分组组织的候选服务智能体列表。"
            "每个分组对应一个查询维度（如子任务、类别等）。"
            "若业务无需分组，仍应返回包含单个默认分组的数组，以保持结构统一。"
        ),
    )

    status: Optional[Literal["ok", "timeout", "error"]] = Field(
        default=None,
        description=(
            "该链路的响应状态。"
            "ok: 成功返回结果；timeout: 下游节点超时；error: 其它错误。"
        ),
    )

    duration_ms: Optional[int] = Field(
        default=None,
        alias="durationMs",
        description="该链路的总耗时（毫秒）。",
        ge=0,
    )

    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="额外的链路级扩展信息。",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# DiscoveryResult 发现结果
# =============================================================================


class DiscoveryResult(BaseModel):
    """
    发现结果。
    """

    acs_map: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict,
        alias="acsMap",
        description=(
            "发现结果中涉及的智能体 ACS 数据。"
            "键为智能体身份码（AIC），值为该智能体的完整能力描述（ACS）。"
            "至少包含 agents 中所有 AIC 的 ACS 数据；"
            "发现服务器可按需包含 routes 中出现的额外 AIC，但不做强制要求。"
            "DiscoveryAgentSkill 中的 aic 字段可作为此映射的查找键。"
        ),
    )

    agents: List[DiscoveryAgentGroup] = Field(
        default_factory=list,
        description=(
            "发现到的候选智能体列表。"
            "当进行了多路聚合时为去重排序后的结果，便于客户端直接使用；"
            "未进行聚合时与 routes 中唯一路径的 agentGroups 一致。"
        ),
    )

    routes: Optional[List[DiscoveryRoute]] = Field(
        default=None,
        description=(
            "下游响应路由集合（可选）。"
            "每条路由对应一次链式或聚合转发路径及其候选技能。"
            "主要用于调试和归因，客户端正常使用时可只关注 agents 和 acsMap。"
        ),
    )

    model_config = ConfigDict(populate_by_name=True)

    alive_map: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None,
        alias="aliveMap",
        description=(
            "AMP alive-sync 注入的在线状态映射（可选）。"
            "键为 AIC，值为对应的 AliveView 输出字典（含 alive、lastSeenAt 等字段）。"
            "仅本地产出的结果（非转发透传）携带此字段；"
            "转发结果原样透传上游的 aliveMap，不覆盖。"
            "alive-sync 未启用或无数据时为 None，序列化时通过 exclude_none 省略。"
        ),
    )

    _alive_enrichable: bool = PrivateAttr(default=False)
    """内部标记：True 表示此结果由本地 DiscoveryService 产出，可注入 aliveMap。
    转发回传结果（来自下游）保持 False，防止本地覆盖透传的 aliveMap（ADP §4.2.3）。
    不序列化（PrivateAttr）。
    """

    def iter_agent_skills(
        self,
    ) -> Iterator[Tuple[str, Dict[str, Any], DiscoveryAgentSkill, str]]:
        """
        遍历所有分组中的智能体技能，自动关联 acsMap 中的 ACS 数据。

        Yields:
            (aic, acs_data, agent_skill, group) 四元组：
            - aic: 智能体身份码
            - acs_data: 从 acsMap 中查找到的 ACS 字典，未找到时为空字典
            - agent_skill: DiscoveryAgentSkill 对象
            - group: 所属分组标识
        """
        acs_map = self.acs_map or {}
        for agent_group in self.agents:
            for agent_skill in agent_group.agent_skills:
                acs_data = acs_map.get(agent_skill.aic, {})
                yield agent_skill.aic, acs_data, agent_skill, agent_group.group


# =============================================================================
# ErrorDetail 通用错误信息
# =============================================================================


class ErrorDetail(BaseModel):
    """
    通用错误信息对象。
    """

    code: int = Field(
        ...,
        description="错误代码。",
        examples=[40001, 50801],
    )

    message: str = Field(
        ...,
        description="错误消息，描述错误的简要信息。",
        examples=["MissingQuery", "ForwardLoopDetected"],
    )

    data: Optional[Any] = Field(
        default=None,
        description="可选的错误数据，提供更多的错误细节和上下文信息。",
    )

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# DiscoveryResponse 发现响应
# =============================================================================


class DiscoveryResponse(BaseModel):
    """
    ADP 发现响应数据结构。

    遵循 CommonResponse 规范：调用成功时包含 result 字段，
    调用失败时包含 error 字段，二者互斥。
    """

    result: Optional[DiscoveryResult] = Field(
        default=None,
        description="请求结果。调用成功时返回匹配到的服务智能体列表。",
    )

    error: Optional[ErrorDetail] = Field(
        default=None,
        description="错误信息。调用失败时包含错误对象。",
    )

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def validate_result_or_error(self) -> "DiscoveryResponse":
        """确保 result 和 error 互斥。"""
        if self.result is not None and self.error is not None:
            raise ValueError("result 和 error 字段互斥，不能同时存在")
        if self.result is None and self.error is None:
            raise ValueError("result 和 error 字段必须至少存在一个")
        return self

    @classmethod
    def success(cls, result: DiscoveryResult) -> "DiscoveryResponse":
        """构建成功响应。"""
        return cls(result=result)

    @classmethod
    def failure(
        cls,
        code: int,
        message: str,
        data: Optional[Any] = None,
    ) -> "DiscoveryResponse":
        """构建失败响应。"""
        return cls(error=ErrorDetail(code=code, message=message, data=data))

    def is_success(self) -> bool:
        """判断是否为成功响应。"""
        return self.result is not None

    def is_error(self) -> bool:
        """判断是否为错误响应。"""
        return self.error is not None

    def get_adp_error(self) -> Optional["ADPError"]:
        """
        将响应中的 error 字段转换为 ADPError 异常对象。

        如果响应为成功响应（无 error），返回 None。
        如果 error.code 是已知的 ADPErrorCode，返回对应的 ADPError；
        否则仍返回 ADPError，但 code 保持为原始整数值。

        Returns:
            ADPError 实例或 None。
        """
        if not self.error:
            return None
        from .errors import ADPError as _ADPError, ADPErrorCode as _ADPErrorCode

        try:
            code = _ADPErrorCode(self.error.code)
        except ValueError:
            code = self.error.code
        return _ADPError(
            code=code,
            message=self.error.message,
            data=self.error.data,
        )

    def to_json(self, **kwargs) -> str:
        """序列化为 JSON 字符串。"""
        return self.model_dump_json(by_alias=True, exclude_none=True, **kwargs)

    def to_dict(self, **kwargs) -> Dict[str, Any]:
        """序列化为字典。"""
        return self.model_dump(by_alias=True, exclude_none=True, **kwargs)

    @classmethod
    def from_json(cls, json_str: str) -> "DiscoveryResponse":
        """从 JSON 字符串反序列化。"""
        return cls.model_validate_json(json_str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveryResponse":
        """从字典反序列化。"""
        return cls.model_validate(data)
