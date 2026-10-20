"""生成 ADP（acps_sdk.adp）的跨语言对照样例。

输出到 test/fixtures/python/adp/：
- exports.json      `acps_sdk.adp.__all__`
- constants.json    全部常量的值
- errors.json       错误码、映射表、ADPError / make_error_response / get_http_status_for_error 的行为
- models.json       各模型的合法输入 → model_dump(by_alias=True, exclude_none=True, mode="json") 与 to_json 文本；
                    非法输入 → 是否抛错与 pydantic 的错误位置/类型/消息
- divergences.json  Python 接受、Node 版有意不接受的输入（pydantic 宽松类型转换、populate_by_name）
- methods.json      DiscoveryRequest / DiscoveryResult / DiscoveryResponse 上的方法
- validators.json   validators.py 中每个函数的输入输出

运行：npm run fixtures:python（或单独运行本脚本）。输出完全确定。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from _common import write_fixture  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

import acps_sdk.adp as adp  # noqa: E402
from acps_sdk.adp import constants as adp_constants  # noqa: E402
from acps_sdk.adp import (  # noqa: E402
    ADP_ERROR_HTTP_STATUS,
    ADP_ERROR_NAMES,
    ADPError,
    ADPErrorCode,
    DiscoveryAgentGroup,
    DiscoveryAgentSkill,
    DiscoveryContext,
    DiscoveryFilter,
    DiscoveryRequest,
    DiscoveryResponse,
    DiscoveryResult,
    DiscoveryRoute,
    ErrorDetail,
    FilterCondition,
    FilterOperator,
    allocate_fanout_budget,
    build_forwarded_request,
    get_http_status_for_error,
    make_error_response,
    should_continue_forwarding,
    validate_discovery_request,
    validate_fanout_budget,
    validate_forward_chain,
    validate_trusted_target,
)

PKG = "adp"

MODELS: Dict[str, type[BaseModel]] = {
    "FilterCondition": FilterCondition,
    "DiscoveryFilter": DiscoveryFilter,
    "DiscoveryContext": DiscoveryContext,
    "DiscoveryRequest": DiscoveryRequest,
    "DiscoveryAgentSkill": DiscoveryAgentSkill,
    "DiscoveryAgentGroup": DiscoveryAgentGroup,
    "DiscoveryRoute": DiscoveryRoute,
    "DiscoveryResult": DiscoveryResult,
    "ErrorDetail": ErrorDetail,
    "DiscoveryResponse": DiscoveryResponse,
}


# =============================================================================
# 通用工具
# =============================================================================


def dump(model: BaseModel) -> Any:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


def errors_of(exc: ValidationError) -> List[Dict[str, Any]]:
    return [{"loc": list(err["loc"]), "type": err["type"], "msg": err["msg"]} for err in exc.errors()]


def model_case(model: str, name: str, data: Any, *, compare_locs: bool = True) -> Dict[str, Any]:
    """compare_locs=False：错误位置含 pydantic 的 Union 成员标签，Node 侧只比对「是否抛错」。"""
    cls = MODELS[model]
    case: Dict[str, Any] = {"model": model, "name": name, "input": data}
    try:
        instance = cls.model_validate(data)
    except ValidationError as exc:
        case.update({"ok": False, "compareLocs": compare_locs, "errors": errors_of(exc)})
        return case
    case.update(
        {
            "ok": True,
            "dump": dump(instance),
            "json": instance.model_dump_json(by_alias=True, exclude_none=True),
        }
    )
    return case


def adp_error_info(exc: ADPError) -> Dict[str, Any]:
    return {
        "kind": "ADPError",
        "code": int(exc.code),
        "message": exc.message,
        "data": exc.data,
        "httpStatus": exc.http_status,
        "body": exc.to_error_body(),
    }


def run(fn: Callable[[], Any], encode: Callable[[Any], Any] = lambda value: value) -> Dict[str, Any]:
    """执行函数，记录返回值或异常。"""
    try:
        value = fn()
    except ADPError as exc:
        return {"error": adp_error_info(exc)}
    except ValidationError as exc:
        return {"error": {"kind": "ValidationError", "errors": errors_of(exc)}}
    except ZeroDivisionError as exc:
        return {"error": {"kind": "ZeroDivisionError", "message": str(exc)}}
    except ValueError as exc:
        return {"error": {"kind": "ValueError", "message": str(exc)}}
    return {"result": encode(value)}


def make_request(data: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None) -> DiscoveryRequest:
    """先按线上格式解析，再按线上字段名直接赋值（不经校验，等价于 Python 里对模型属性赋值）。"""
    request = DiscoveryRequest.from_dict(data)
    by_alias = {(info.alias or field): field for field, info in DiscoveryRequest.model_fields.items()}
    for key, value in (overrides or {}).items():
        target = by_alias[key]
        if key == "filter" and value is not None:
            value = DiscoveryFilter.model_validate(value)
        setattr(request, target, value)
    return request


# =============================================================================
# exports / constants
# =============================================================================


def gen_exports() -> None:
    write_fixture(PKG, "exports", {"all": list(adp.__all__)})


def gen_constants() -> None:
    names = [name for name in adp.__all__ if name.isupper() and hasattr(adp_constants, name)]
    values = {}
    for name in names:
        value = getattr(adp_constants, name)
        values[name] = list(value) if isinstance(value, tuple) else value
    write_fixture(PKG, "constants", values)
    write_fixture(PKG, "filter_operators", [[member.name, member.value] for member in FilterOperator])


# =============================================================================
# errors
# =============================================================================


def gen_errors() -> None:
    codes = {member.name: member.value for member in ADPErrorCode}
    classification = {
        member.name: {
            "isRedirect": member.is_redirect(),
            "isRetryable": member.is_retryable(),
            "isClientError": member.is_client_error(),
            "isForwardError": member.is_forward_error(),
        }
        for member in ADPErrorCode
    }

    error_cases = []
    for label, code, message, data in [
        ("known code, default message", ADPErrorCode.MISSING_QUERY, None, None),
        ("known code, custom message, dict data", ADPErrorCode.FORWARD_FANOUT_EXCEEDED, "Fan-out budget exhausted",
         {"availableBudget": 1, "requiredBranches": 3}),
        ("empty message falls back to name", ADPErrorCode.CAPACITY_REDIRECT, "", None),
        ("falsy data 0 is kept", ADPErrorCode.INTERNAL_ERROR, "boom", 0),
        ("falsy data false is kept", ADPErrorCode.TENANT_RATE_LIMITED, None, False),
        ("empty dict data is kept", ADPErrorCode.FORWARD_LOOP_DETECTED, "loop", {}),
        ("list data", ADPErrorCode.FORWARD_CHAIN_TAMPERED, None, ["a", None]),
        ("unknown code in client range", 40099, None, None),
        ("unknown code in forward range", 50899, "custom", "detail"),
        ("unknown code", 12345, None, None),
    ]:
        exc = ADPError(code=code, message=message, data=data)
        error_cases.append(
            {
                "name": label,
                "args": {"code": int(code), "message": message, "data": data},
                "message": exc.message,
                "str": str(exc),
                "httpStatus": exc.http_status,
                "errorBody": exc.to_error_body(),
                "responseDict": exc.to_response_dict(),
                "isRedirect": exc.is_redirect(),
                "isRetryable": exc.is_retryable(),
                "isClientError": exc.is_client_error(),
                "isForwardError": exc.is_forward_error(),
            }
        )

    make_error_cases = [
        {"args": {"code": 40001, "message": None, "data": None},
         "result": make_error_response(ADPErrorCode.MISSING_QUERY)},
        {"args": {"code": 50804, "message": "Fan-out budget exhausted",
                  "data": {"availableBudget": 1, "requiredBranches": 3}},
         "result": make_error_response(ADPErrorCode.FORWARD_FANOUT_EXCEEDED, message="Fan-out budget exhausted",
                                       data={"availableBudget": 1, "requiredBranches": 3})},
        {"args": {"code": 99999, "message": None, "data": None}, "result": make_error_response(99999)},
    ]

    http_status_cases = [
        {"code": code, "status": get_http_status_for_error(code)}
        for code in [*codes.values(), 40099, 99999, 0]
    ]

    write_fixture(
        PKG,
        "errors",
        {
            "codes": codes,
            "classification": classification,
            "names": {str(int(k)): v for k, v in ADP_ERROR_NAMES.items()},
            "httpStatus": {str(int(k)): v for k, v in ADP_ERROR_HTTP_STATUS.items()},
            "adpErrors": error_cases,
            "makeErrorResponse": make_error_cases,
            "getHttpStatusForError": http_status_cases,
        },
    )


# =============================================================================
# models
# =============================================================================

SKILL = {"aic": "AIC-001", "skillId": "food-rec", "ranking": 1}
GROUP = {"group": "美食推荐", "agentSkills": [SKILL]}


def gen_models() -> None:
    cases: List[Dict[str, Any]] = []
    add = cases.append

    # ── FilterCondition ──
    add(model_case("FilterCondition", "bool value", {"field": "active", "op": "eq", "value": True}))
    add(model_case("FilterCondition", "null value is dropped", {"field": "skills.tags", "op": "exists", "value": None}))
    add(model_case("FilterCondition", "missing value", {"field": "x", "op": "exists"}))
    add(model_case("FilterCondition", "falsy values are kept", {"field": "x", "op": "size", "value": 0}))
    add(model_case("FilterCondition", "false value is kept", {"field": "x", "op": "eq", "value": False}))
    add(model_case("FilterCondition", "dict value keeps inner null",
                   {"field": "entityMeta", "op": "hasKey", "value": {"k": None, "n": [1, None]}}))
    add(model_case("FilterCondition", "between tuple", {"field": "version", "op": "between", "value": ["1.0.0", "2.0.0"]}))
    for op in ["notContains", "startsWithCs", "hasAllKeys", "sizeLte", "in"]:
        add(model_case("FilterCondition", f"operator {op}", {"field": "f", "op": op, "value": ["a"]}))
    add(model_case("FilterCondition", "unknown operator", {"field": "f", "op": "regex", "value": "a"}))
    add(model_case("FilterCondition", "operator is case sensitive", {"field": "f", "op": "EQ", "value": "a"}))
    add(model_case("FilterCondition", "missing field and op", {"value": 1}))
    add(model_case("FilterCondition", "field must be string", {"field": 1, "op": "eq"}))

    # ── DiscoveryFilter ──
    add(model_case("DiscoveryFilter", "empty filter gets default logic", {}))
    add(model_case("DiscoveryFilter", "explicit null logic is dropped", {"logic": None}))
    add(model_case("DiscoveryFilter", "null conditions and groups", {"conditions": None, "groups": None, "logic": "or"}))
    add(model_case("DiscoveryFilter", "nested groups", {
        "conditions": [{"field": "active", "op": "eq", "value": True}],
        "groups": [
            {"logic": "or", "conditions": [{"field": "provider.countryCode", "op": "in", "value": ["CN", "US"]}]},
            {"logic": "not", "groups": [{"conditions": [{"field": "skills.tags", "op": "anyOf", "value": ["x"]}]}]},
        ],
    }))
    add(model_case("DiscoveryFilter", "invalid logic", {"logic": "xor"}))
    add(model_case("DiscoveryFilter", "logic is case sensitive", {"logic": "AND"}))
    add(model_case("DiscoveryFilter", "invalid nested condition", {
        "groups": [{"groups": [{"conditions": [{"field": "a", "op": "bad"}]}]}],
    }))
    add(model_case("DiscoveryFilter", "groups must be a list", {"groups": {"logic": "and"}}))

    # ── DiscoveryContext ──
    add(model_case("DiscoveryContext", "full context", {
        "conversationId": "conv-123",
        "recentTurns": ["上一轮询问了北京菜系", "用户偏好健康饮食"],
        "userProfile": {"city": "北京", "budget": "medium", "age": None},
        "metadata": {"nested": {"k": None}},
    }))
    add(model_case("DiscoveryContext", "all null", {
        "conversationId": None, "recentTurns": None, "userProfile": None, "metadata": None,
    }))
    add(model_case("DiscoveryContext", "recentTurns must be strings", {"recentTurns": ["a", 1]}))
    add(model_case("DiscoveryContext", "userProfile must be a dict", {"userProfile": ["a"]}))

    # ── DiscoveryRequest ──
    req = "DiscoveryRequest"
    add(model_case(req, "minimal explicit", {"query": "我需要一个可以做北京美食推荐的智能体"}))
    add(model_case(req, "full request", {
        "type": "explicit",
        "query": "翻译智能体",
        "context": {"conversationId": "c1", "recentTurns": ["t1"], "userProfile": {"city": "北京"}},
        "limit": 5,
        "filter": {"conditions": [{"field": "active", "op": "eq", "value": True}], "logic": "and"},
        "forwardDepthLimit": 3,
        "forwardFanoutLimit": 4,
        "forwardFanoutRemaining": 0,
        "forwardChain": ["AIC-DS-A"],
        "forwardTrustedServers": ["AIC-DS-B", "AIC-DS-C"],
        "forwardSignatures": ["sig-a"],
        "forwardEachTimeoutMs": 5000,
        "forwardTotalTimeoutMs": 30000,
    }))
    add(model_case(req, "keys out of order are emitted in field order", {
        "forwardTotalTimeoutMs": 1, "forwardChain": [], "query": "q", "type": "exploratory", "limit": 1,
    }))
    add(model_case(req, "unknown keys are ignored", {"query": "q", "unknownKey": 1, "extra": {"a": 1}}))
    add(model_case(req, "all optional fields null", {
        "query": "q", "context": None, "limit": None, "filter": None, "forwardDepthLimit": None,
        "forwardFanoutLimit": None, "forwardFanoutRemaining": None, "forwardChain": None,
        "forwardTrustedServers": None, "forwardSignatures": None, "forwardEachTimeoutMs": None,
        "forwardTotalTimeoutMs": None,
    }))
    for query_type in ["exploratory", "trending", "filtered"]:
        add(model_case(req, f"{query_type} without query", {"type": query_type}))
    add(model_case(req, "null type disables the query check", {"type": None}))
    add(model_case(req, "null type keeps query", {"type": None, "query": "q"}))
    add(model_case(req, "missing query defaults to explicit", {}))
    add(model_case(req, "explicit without query", {"type": "explicit"}))
    add(model_case(req, "explicit with null query", {"type": "explicit", "query": None}))
    add(model_case(req, "explicit with empty query", {"query": ""}))
    add(model_case(req, "explicit with whitespace query", {"query": " \t\n　"}))
    add(model_case(req, "python whitespace U+001C/U+0085 is blank", {"query": "\x1c\x1f\x85"}))
    add(model_case(req, "BOM is not python whitespace", {"query": "﻿"}))
    add(model_case(req, "control character round-trips through json", {"query": "a\x1cb\x7f c"}))
    add(model_case(req, "invalid type", {"type": "foo", "query": "q"}))
    add(model_case(req, "empty type string", {"type": ""}))
    add(model_case(req, "type is case sensitive", {"type": "Explicit", "query": "q"}))
    add(model_case(req, "invalid type with quote", {"type": "it's", "query": "q"}))
    add(model_case(req, "type must be string", {"type": 5, "query": "q"}))
    add(model_case(req, "invalid type skips query check", {"type": "foo"}))
    add(model_case(req, "query must be string", {"query": 5}))
    add(model_case(req, "limit lower bound", {"query": "q", "limit": 1}))
    add(model_case(req, "limit below 1", {"query": "q", "limit": 0}))
    add(model_case(req, "limit fractional", {"query": "q", "limit": 1.5}))
    add(model_case(req, "depth and fanout bounds", {"query": "q", "forwardDepthLimit": 5, "forwardFanoutLimit": 1}))
    add(model_case(req, "depth and fanout out of range", {"query": "q", "forwardDepthLimit": 6, "forwardFanoutLimit": 0}))
    add(model_case(req, "depth below minimum", {"query": "q", "forwardDepthLimit": 0, "forwardFanoutLimit": 6}))
    add(model_case(req, "negative fanout remaining", {"query": "q", "forwardFanoutRemaining": -1}))
    add(model_case(req, "zero timeouts", {"query": "q", "forwardEachTimeoutMs": 0, "forwardTotalTimeoutMs": 0}))
    add(model_case(req, "field errors suppress the query check", {"query": "", "limit": 0}))
    add(model_case(req, "field errors suppress the query check (missing)", {"forwardDepthLimit": 9}))
    add(model_case(req, "forwardChain must be strings", {"query": "q", "forwardChain": ["a", 1]}))
    add(model_case(req, "invalid nested filter", {
        "query": "q", "filter": {"logic": "xor", "conditions": [{"field": "a", "op": "bad"}]},
    }))
    add(model_case(req, "invalid context", {"query": "q", "context": {"recentTurns": "x"}}))
    add(model_case(req, "filter must be object", {"query": "q", "filter": "active=true"}))
    add(model_case(req, "not an object", ["query"]))

    # ── DiscoveryAgentSkill / DiscoveryAgentGroup / DiscoveryRoute ──
    add(model_case("DiscoveryAgentSkill", "skill", {**SKILL, "memo": "score=0.9"}))
    add(model_case("DiscoveryAgentSkill", "missing skillId and ranking", {"aic": "a"}))
    add(model_case("DiscoveryAgentSkill", "ranking must be integer", {**SKILL, "ranking": 1.5}))
    add(model_case("DiscoveryAgentGroup", "group default skills", {"group": ""}))
    add(model_case("DiscoveryAgentGroup", "group with skills", GROUP))
    add(model_case("DiscoveryAgentGroup", "missing group", {"agentSkills": []}))
    add(model_case("DiscoveryRoute", "route", {
        "forwardChain": ["AIC-DS-A", "AIC-DS-B"], "agentGroups": [GROUP], "status": "ok", "durationMs": 150,
        "metadata": {"server": "b", "note": None},
    }))
    add(model_case("DiscoveryRoute", "route defaults", {"forwardChain": []}))
    for status in ["timeout", "error"]:
        add(model_case("DiscoveryRoute", f"route status {status}", {"forwardChain": ["a"], "status": status}))
    add(model_case("DiscoveryRoute", "invalid route status", {"forwardChain": ["a"], "status": "failed"}))
    add(model_case("DiscoveryRoute", "negative duration", {"forwardChain": ["a"], "durationMs": -1}))
    add(model_case("DiscoveryRoute", "zero duration", {"forwardChain": ["a"], "durationMs": 0}))
    add(model_case("DiscoveryRoute", "missing forwardChain", {"agentGroups": []}))

    # ── DiscoveryResult ──
    add(model_case("DiscoveryResult", "empty result", {}))
    add(model_case("DiscoveryResult", "full result", {
        "acsMap": {"AIC-001": {"name": "FoodAgent", "description": "美食推荐智能体", "iconUrl": None}},
        "agents": [GROUP],
        "routes": [{"forwardChain": ["AIC-DS-A"], "agentGroups": [GROUP], "status": "ok", "durationMs": 150}],
        "aliveMap": {"AIC-001": {"alive": True, "aliveLastSeenAt": None}},
    }))
    add(model_case("DiscoveryResult", "aliveMap before routes in input", {
        "aliveMap": {"AIC-001": {"alive": False}}, "routes": [], "agents": [], "acsMap": {},
    }))
    add(model_case("DiscoveryResult", "null aliveMap and routes", {"aliveMap": None, "routes": None}))
    add(model_case("DiscoveryResult", "acsMap values must be dicts", {"acsMap": {"AIC-001": "x"}}))
    add(model_case("DiscoveryResult", "aliveMap values must be dicts", {"aliveMap": {"AIC-001": True}}))

    # ── ErrorDetail / DiscoveryResponse ──
    add(model_case("ErrorDetail", "error with data", {"code": 40001, "message": "MissingQuery", "data": {"k": None}}))
    add(model_case("ErrorDetail", "error with null data", {"code": 50801, "message": "ForwardLoopDetected", "data": None}))
    add(model_case("ErrorDetail", "error with string data", {"code": 1, "message": "m", "data": "detail"}))
    add(model_case("ErrorDetail", "missing message", {"code": 1}))
    resp = "DiscoveryResponse"
    add(model_case(resp, "success response", {"result": {"agents": [GROUP], "acsMap": {"AIC-001": {"name": "n"}}}}))
    add(model_case(resp, "empty result is success", {"result": {}}))
    add(model_case(resp, "error response", {"error": {"code": 40001, "message": "MissingQuery"}}))
    add(model_case(resp, "null result with error", {"result": None, "error": {"code": 1, "message": "m", "data": [1]}}))
    add(model_case(resp, "both result and error", {"result": {}, "error": {"code": 1, "message": "m"}}))
    add(model_case(resp, "neither result nor error", {}))
    add(model_case(resp, "both null", {"result": None, "error": None}))
    add(model_case(resp, "field errors suppress the exclusivity check", {"result": {}, "error": {"code": "x"}}))
    add(model_case(resp, "invalid nested skill", {"result": {"agents": [{"group": "g", "agentSkills": [{"aic": "a"}]}]}}))

    write_fixture(PKG, "models", cases)


def gen_divergences() -> None:
    """Python 接受、Node 版有意拒绝/忽略的输入（与 porting-notes 的约定一致）。"""
    cases = [
        # pydantic 宽松模式（lax）的类型转换
        model_case("DiscoveryRequest", "lax: numeric string to int", {"query": "q", "limit": "5"}),
        model_case("DiscoveryRequest", "lax: bool to int", {"query": "q", "limit": True}),
        model_case("DiscoveryAgentSkill", "lax: bool ranking", {"aic": "a", "skillId": "s", "ranking": True}),
        model_case("ErrorDetail", "lax: numeric string code", {"code": "40001", "message": "m"}),
    ]
    for case in cases:
        case["kind"] = "lax"
    by_name = [
        # populate_by_name=True：Python 也接受 snake_case 字段名
        model_case("DiscoveryRequest", "populate_by_name: snake_case keys",
                   {"query": "q", "forward_chain": ["AIC-A"], "forward_depth_limit": 2}),
        model_case("DiscoveryAgentSkill", "populate_by_name: skill_id", {"aic": "a", "skill_id": "s", "ranking": 1}),
    ]
    for case in by_name:
        case["kind"] = "populateByName"
    write_fixture(PKG, "divergences", cases + by_name)


# =============================================================================
# methods
# =============================================================================


def effective_values(request: DiscoveryRequest) -> Dict[str, int]:
    return {
        "getEffectiveDepthLimit": request.get_effective_depth_limit(),
        "getEffectiveFanoutLimit": request.get_effective_fanout_limit(),
        "getEffectiveFanoutRemaining": request.get_effective_fanout_remaining(),
        "getEffectiveEachTimeoutMs": request.get_effective_each_timeout_ms(),
        "getEffectiveTotalTimeoutMs": request.get_effective_total_timeout_ms(),
        "getForwardChainLength": request.get_forward_chain_length(),
    }


def gen_methods() -> None:
    request_cases = []
    for name, data, overrides in [
        ("defaults", {"query": "q"}, None),
        ("explicit values", {"query": "q", "forwardDepthLimit": 5, "forwardFanoutLimit": 4,
                             "forwardEachTimeoutMs": 100, "forwardTotalTimeoutMs": 200,
                             "forwardChain": ["a", "b"]}, None),
        ("fanout remaining zero is kept", {"query": "q", "forwardFanoutLimit": 3, "forwardFanoutRemaining": 0}, None),
        ("fanout remaining falls back to limit", {"query": "q", "forwardFanoutLimit": 3}, None),
        ("empty chain", {"query": "q", "forwardChain": []}, None),
        # 赋值绕过校验得到的 0：get_effective_* 用 `or`，0 回退到默认值
        ("zero values fall back to defaults", {"query": "q"},
         {"forwardDepthLimit": 0, "forwardFanoutLimit": 0, "forwardEachTimeoutMs": 0, "forwardTotalTimeoutMs": 0}),
    ]:
        request = make_request(data, overrides)
        request_cases.append({"name": name, "input": data, "overrides": overrides, "expected": effective_values(request)})

    serialization_cases = []
    for name, data in [
        ("request to_dict/to_json", {"query": "北京", "forwardChain": ["a"], "context": {"userProfile": {"k": None}}}),
        ("request with null type", {"type": None, "query": "q"}),
        ("request with default filter logic", {"type": "filtered", "filter": {}}),
    ]:
        request = DiscoveryRequest.from_dict(data)
        serialization_cases.append({
            "name": name,
            "input": data,
            "toDict": request.to_dict(),
            "toJson": request.to_json(),
            "fromJsonOfToJson": DiscoveryRequest.from_json(request.to_json()).to_dict(),
        })

    result_data = {
        "acsMap": {"AIC-001": {"name": "FoodAgent"}, "AIC-002": {"name": "TeaAgent"}},
        "agents": [
            {"group": "美食推荐", "agentSkills": [
                {"aic": "AIC-001", "skillId": "food-rec", "ranking": 1},
                {"aic": "AIC-404", "skillId": "missing-acs", "ranking": 2},
            ]},
            {"group": "", "agentSkills": []},
            {"group": "茶饮", "agentSkills": [{"aic": "AIC-002", "skillId": "tea", "ranking": 1, "memo": "m"}]},
        ],
    }
    result = DiscoveryResult.model_validate(result_data)
    iter_cases = [
        {
            "name": "iter_agent_skills joins acsMap",
            "input": result_data,
            "expected": [[aic, acs, dump(skill), group] for aic, acs, skill, group in result.iter_agent_skills()],
        },
        {
            "name": "iter_agent_skills on empty result",
            "input": {},
            "expected": [list(item) for item in DiscoveryResult().iter_agent_skills()],
        },
    ]

    success = DiscoveryResponse.success(result=DiscoveryResult.model_validate(result_data))
    response_cases = {
        "success": {
            "input": result_data,
            "toDict": success.to_dict(),
            "toJson": success.to_json(),
            "isSuccess": success.is_success(),
            "isError": success.is_error(),
            "adpError": None,
        },
        "failures": [],
        "successWithNone": run(lambda: DiscoveryResponse.success(result=None)),  # type: ignore[arg-type]
        "failureWithBadCode": run(lambda: DiscoveryResponse.failure("x", "m")),  # type: ignore[arg-type]
    }
    for code, message, data in [
        (40001, "MissingQuery", None),
        (50804, "Fan-out budget exhausted", {"availableBudget": 1, "requiredBranches": 3}),
        (12345, "Custom", "detail"),
        (40099, "", None),
    ]:
        failure = DiscoveryResponse.failure(code, message, data)
        adp_error = failure.get_adp_error()
        response_cases["failures"].append({
            "args": {"code": code, "message": message, "data": data},
            "toDict": failure.to_dict(),
            "toJson": failure.to_json(),
            "isSuccess": failure.is_success(),
            "isError": failure.is_error(),
            "adpError": {
                "code": int(adp_error.code),
                "message": adp_error.message,
                "data": adp_error.data,
                "httpStatus": adp_error.http_status,
                "isClientError": adp_error.is_client_error(),
                "isForwardError": adp_error.is_forward_error(),
            },
        })

    write_fixture(PKG, "methods", {
        "request": request_cases,
        "serialization": serialization_cases,
        "iterAgentSkills": iter_cases,
        "response": response_cases,
    })


# =============================================================================
# validators
# =============================================================================


def request_case(name: str, data: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None, **extra: Any):
    return {"name": name, "input": data, "overrides": overrides, **extra}


def nested_filter(depth: int) -> Dict[str, Any]:
    node: Dict[str, Any] = {"conditions": [{"field": "active", "op": "eq", "value": True}]}
    for _ in range(depth - 1):
        node = {"groups": [{"logic": "or"}, node]}
    return node


def gen_validators() -> None:
    validate_request_cases = []
    for case in [
        request_case("valid explicit", {"query": "q"}),
        request_case("trending without query", {"type": "trending"}),
        request_case("null type without query", {"type": None}),
        request_case("blank query bypassing the model", {"query": "q"}, {"query": "   "}),
        request_case("python-blank query bypassing the model", {"query": "q"}, {"query": "\x1c\x85"}),
        request_case("BOM query is not blank", {"query": "﻿"}),
        request_case("null query bypassing the model", {"query": "q"}, {"query": None}),
        request_case("depth limit too large", {"query": "q"}, {"forwardDepthLimit": 6}),
        request_case("depth limit zero", {"query": "q"}, {"forwardDepthLimit": 0}),
        request_case("fanout limit too large", {"query": "q"}, {"forwardFanoutLimit": 9}),
        request_case("fanout limit negative", {"query": "q"}, {"forwardFanoutLimit": -1}),
        request_case("depth checked before fanout", {"query": "q"}, {"forwardDepthLimit": 6, "forwardFanoutLimit": 9}),
        request_case("filter depth 3 is allowed", {"query": "q", "filter": nested_filter(3)}),
        request_case("filter depth 4 is rejected", {"query": "q", "filter": nested_filter(4)}),
        request_case("filter with empty groups", {"query": "q", "filter": {"groups": []}}),
    ]:
        request = make_request(case["input"], case["overrides"])
        case.update(run(lambda: validate_discovery_request(request)))
        validate_request_cases.append(case)

    forward_chain_cases = []
    for case in [
        request_case("first hop without chain", {"query": "q"}, currentServerAic="AIC-DS-A", senderAic=None),
        request_case("first hop with sender but no chain", {"query": "q"}, currentServerAic="AIC-DS-A",
                     senderAic="AIC-CLIENT"),
        request_case("valid second hop", {"query": "q", "forwardChain": ["AIC-DS-A"]}, currentServerAic="AIC-DS-B",
                     senderAic="AIC-DS-A"),
        request_case("sender omitted skips integrity check", {"query": "q", "forwardChain": ["AIC-DS-A"]},
                     currentServerAic="AIC-DS-B", senderAic=None),
        request_case("loop detected", {"query": "q", "forwardChain": ["AIC-DS-A", "AIC-DS-B"]},
                     currentServerAic="AIC-DS-A", senderAic="AIC-DS-B"),
        request_case("loop detected with quotes in AIC", {"query": "q", "forwardChain": ["it's", 'say "hi"']},
                     currentServerAic="it's", senderAic=None),
        request_case("loop checked before depth", {"query": "q", "forwardDepthLimit": 1, "forwardChain": ["AIC-DS-A"]},
                     currentServerAic="AIC-DS-A", senderAic=None),
        request_case("depth exceeded with default limit", {"query": "q", "forwardChain": ["A", "B", "C"]},
                     currentServerAic="D", senderAic="C"),
        request_case("depth exceeded with explicit limit", {"query": "q", "forwardDepthLimit": 2,
                                                            "forwardChain": ["A", "B"]},
                     currentServerAic="C", senderAic="B"),
        request_case("chain tampered", {"query": "q", "forwardChain": ["AIC-DS-A", "AIC-DS-X"]},
                     currentServerAic="AIC-DS-C", senderAic="AIC-DS-B"),
        request_case("chain tampered with backslash", {"query": "q", "forwardChain": ["a\\b"]},
                     currentServerAic="c", senderAic="a'b"),
        request_case("empty chain with zero depth limit", {"query": "q"}, {"forwardDepthLimit": 0},
                     currentServerAic="A", senderAic=None),
    ]:
        request = make_request(case["input"], case["overrides"])
        current, sender = case["currentServerAic"], case["senderAic"]
        case.update(run(lambda: validate_forward_chain(request, current_server_aic=current, sender_aic=sender)))
        forward_chain_cases.append(case)

    fanout_cases = []
    for case in [
        request_case("default budget covers one branch", {"query": "q"}, requiredBranches=1),
        request_case("default budget rejects two branches", {"query": "q"}, requiredBranches=2),
        request_case("limit used when remaining unset", {"query": "q", "forwardFanoutLimit": 4}, requiredBranches=3),
        request_case("remaining overrides limit", {"query": "q", "forwardFanoutLimit": 4, "forwardFanoutRemaining": 2},
                     requiredBranches=3),
        request_case("zero remaining allows zero branches", {"query": "q", "forwardFanoutRemaining": 0},
                     requiredBranches=0),
    ]:
        request = make_request(case["input"], case["overrides"])
        required = case["requiredBranches"]
        case.update(run(lambda: validate_fanout_budget(request, required_branches=required)))
        fanout_cases.append(case)

    trusted_cases = []
    for case in [
        request_case("no trusted list", {"query": "q"}, targetAic="X"),
        request_case("empty trusted list", {"query": "q", "forwardTrustedServers": []}, targetAic="X"),
        request_case("trusted target", {"query": "q", "forwardTrustedServers": ["A", "X"]}, targetAic="X"),
        request_case("untrusted target", {"query": "q", "forwardTrustedServers": ["A"]}, targetAic="X"),
    ]:
        request = make_request(case["input"], case["overrides"])
        target = case["targetAic"]
        case.update(run(lambda: validate_trusted_target(request, target_aic=target)))
        trusted_cases.append(case)

    continue_cases = []
    for case in [
        request_case("defaults", {"query": "q"}),
        request_case("equal timeouts", {"query": "q", "forwardEachTimeoutMs": 500, "forwardTotalTimeoutMs": 500}),
        request_case("total below each", {"query": "q", "forwardEachTimeoutMs": 501, "forwardTotalTimeoutMs": 500}),
        request_case("total below default each", {"query": "q", "forwardTotalTimeoutMs": 9999}),
        request_case("zero total falls back to default", {"query": "q", "forwardEachTimeoutMs": 20000},
                     {"forwardTotalTimeoutMs": 0}),
    ]:
        request = make_request(case["input"], case["overrides"])
        case.update(run(lambda: should_continue_forwarding(request)))
        continue_cases.append(case)

    build_cases = []
    for case in [
        request_case("first hop", {"query": "翻译智能体", "forwardDepthLimit": 3, "forwardFanoutLimit": 4},
                     args={"currentServerAic": "AIC-DS-A", "fanoutRemainingForBranch": 1}),
        request_case("readme example", {"type": "explicit", "query": "翻译智能体", "forwardDepthLimit": 3,
                                        "forwardFanoutLimit": 4, "forwardChain": ["AIC-DS-A"]},
                     args={"currentServerAic": "AIC-DS-B", "fanoutRemainingForBranch": 0, "elapsedMs": 200,
                           "signature": None, "trustedServers": ["AIC-DS-B", "AIC-DS-C", "AIC-DS-D"]}),
        request_case("appends signature to existing list",
                     {"query": "q", "forwardChain": ["A"], "forwardSignatures": ["sig-a"],
                      "forwardTotalTimeoutMs": 1000, "context": {"userProfile": {"k": None}},
                      "filter": {"conditions": [{"field": "active", "op": "eq", "value": True}]}},
                     args={"currentServerAic": "B", "fanoutRemainingForBranch": 2, "elapsedMs": 250,
                           "signature": "sig-b", "trustedServers": None}),
        request_case("first signature", {"query": "q"},
                     args={"currentServerAic": "A", "fanoutRemainingForBranch": 0, "elapsedMs": 0,
                           "signature": "sig-a", "trustedServers": []}),
        request_case("elapsed beyond total clamps to zero", {"query": "q", "forwardTotalTimeoutMs": 100},
                     args={"currentServerAic": "A", "fanoutRemainingForBranch": 0, "elapsedMs": 500}),
        request_case("null type becomes explicit after round trip", {"type": None, "query": "q"},
                     args={"currentServerAic": "A", "fanoutRemainingForBranch": 0}),
        request_case("null type without query fails the round trip", {"type": None},
                     args={"currentServerAic": "A", "fanoutRemainingForBranch": 0}),
        request_case("zero total timeout fails the round trip", {"query": "q"}, {"forwardTotalTimeoutMs": 0},
                     args={"currentServerAic": "A", "fanoutRemainingForBranch": 0}),
        request_case("filtered request keeps filter logic default", {"type": "filtered", "filter": {"logic": None}},
                     args={"currentServerAic": "A", "fanoutRemainingForBranch": 3, "elapsedMs": 10}),
    ]:
        original = make_request(case["input"], case["overrides"])
        before = original.to_dict()
        args = case["args"]
        outcome = run(
            lambda: build_forwarded_request(
                original,
                current_server_aic=args["currentServerAic"],
                fanout_remaining_for_branch=args["fanoutRemainingForBranch"],
                elapsed_ms=args.get("elapsedMs", 0),
                signature=args.get("signature"),
                trusted_servers=args.get("trustedServers"),
            ),
            encode=lambda forwarded: {"toDict": forwarded.to_dict(), "toJson": forwarded.to_json()},
        )
        case.update(outcome)
        case["originalUnchanged"] = original.to_dict() == before
        build_cases.append(case)

    allocate_cases = []
    for args in [
        {"totalRemaining": 4, "branchCount": 3},
        {"totalRemaining": 4, "branchCount": 3, "strategy": "weighted", "weights": [2, 1, 1]},
        {"totalRemaining": 10, "branchCount": 3},
        {"totalRemaining": 11, "branchCount": 3},
        {"totalRemaining": 3, "branchCount": 3},
        {"totalRemaining": 5, "branchCount": 1},
        {"totalRemaining": 10, "branchCount": 3, "strategy": "weighted", "weights": [1, 1, 1]},
        {"totalRemaining": 10, "branchCount": 3, "strategy": "weighted", "weights": [5, 0, 0]},
        {"totalRemaining": 100, "branchCount": 5, "strategy": "weighted", "weights": [3, 1, 4, 1, 5]},
        {"totalRemaining": 9, "branchCount": 4, "strategy": "weighted", "weights": [1, 2, 2, 1]},
        {"totalRemaining": 7, "branchCount": 2, "strategy": "weighted", "weights": [0, 0]},
        {"totalRemaining": 8, "branchCount": 2, "strategy": "weighted", "weights": [-1, 3]},
        {"totalRemaining": 5, "branchCount": 2, "strategy": "weighted", "weights": None},
        {"totalRemaining": 5, "branchCount": 2, "strategy": "unknown"},
        {"totalRemaining": 5, "branchCount": 3, "strategy": "weighted", "weights": [1, 2]},
        {"totalRemaining": 2, "branchCount": 3},
        {"totalRemaining": 2, "branchCount": 3, "strategy": "weighted", "weights": [1, 1, 1]},
        {"totalRemaining": 3, "branchCount": 0},
        {"totalRemaining": 3, "branchCount": 0, "strategy": "weighted", "weights": []},
        {"totalRemaining": 5, "branchCount": -2},
    ]:
        outcome = run(
            lambda: allocate_fanout_budget(
                total_remaining=args["totalRemaining"],
                branch_count=args["branchCount"],
                strategy=args.get("strategy", "equal"),
                weights=args.get("weights"),
            )
        )
        allocate_cases.append({"args": args, **outcome})

    write_fixture(PKG, "validators", {
        "validateDiscoveryRequest": validate_request_cases,
        "validateForwardChain": forward_chain_cases,
        "validateFanoutBudget": fanout_cases,
        "validateTrustedTarget": trusted_cases,
        "shouldContinueForwarding": continue_cases,
        "buildForwardedRequest": build_cases,
        "allocateFanoutBudget": allocate_cases,
    })


def main() -> None:
    gen_exports()
    gen_constants()
    gen_errors()
    gen_models()
    gen_divergences()
    gen_methods()
    gen_validators()


if __name__ == "__main__":
    main()
