"""生成 ACS（acps_sdk.acs）的跨语言对照样例。

输出到 test/fixtures/python/acs/：
- exports.json      `acps_sdk.acs.__all__`
- models.json       各模型的合法输入 → model_dump(by_alias=True, exclude_none=True, mode="json") 与 to_json 文本；
                    非法输入 → 是否抛错与 pydantic 的错误位置/类型/消息
- divergences.json  Python 接受、Node 版有意不接受的输入（pydantic 宽松类型转换、populate_by_name）
- methods.json      AgentCapabilitySpec.to_dict / to_json / from_json / from_file 的行为
- files/            from_file 用到的样例文件

样例数据取自 ACPs-spec-ACS 第 4 节的两个示例（去掉注释）。
运行：npm run fixtures:python（或单独运行本脚本）。输出完全确定。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent))

from _common import FIXTURE_ROOT, write_fixture  # noqa: E402
from pydantic import BaseModel, TypeAdapter, ValidationError  # noqa: E402

import acps_sdk.acs as acs  # noqa: E402
from acps_sdk.acs.models import (  # noqa: E402
    AgentCapabilities,
    AgentCapabilitySpec,
    AgentEndPoint,
    AgentProvider,
    AgentSkill,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    MQProtocolVersion,
    MutualTLSSecurityScheme,
    OAuth2Flow,
    OAuth2Flows,
    OAuth2SecurityScheme,
    OpenIdConnectSecurityScheme,
    SecurityScheme,
)

PKG = "acs"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

MODELS: Dict[str, Any] = {
    "AgentProvider": AgentProvider,
    "AgentCapabilities": AgentCapabilities,
    "MutualTLSSecurityScheme": MutualTLSSecurityScheme,
    "OpenIdConnectSecurityScheme": OpenIdConnectSecurityScheme,
    "APIKeySecurityScheme": APIKeySecurityScheme,
    "HTTPAuthSecurityScheme": HTTPAuthSecurityScheme,
    "OAuth2Flow": OAuth2Flow,
    "OAuth2Flows": OAuth2Flows,
    "OAuth2SecurityScheme": OAuth2SecurityScheme,
    "SecurityScheme": TypeAdapter(SecurityScheme),
    "AgentEndPoint": AgentEndPoint,
    "AgentSkill": AgentSkill,
    "AgentCapabilitySpec": AgentCapabilitySpec,
}

# ACPs-spec-ACS 4.1 北京城区旅游规划助手示例（原文 messageQueue 为 "rabbitmq:3.*"，不在 MQProtocolVersion 枚举内）
SPEC_EXAMPLE_URBAN: Dict[str, Any] = {
    "aic": "1.2.156.3088.1.34C2.478BDF.3GF546.1.0SEN",
    "active": True,
    "lastModifiedTime": "2025-03-15T16:30:00+08:00",
    "protocolVersion": "02.02",
    "name": "北京城区旅游规划助手",
    "description": "专门负责北京城六区（东城/西城/朝阳/海淀/丰台/石景山）的旅游景点推荐和行程规划。"
    "提供个性化旅游建议，支持亲子游、文化深度游、商务游等多种场景。拒绝超出城区范围的请求，如八达岭长城、古北水镇等郊区景点。",
    "version": "1.2.0",
    "iconUrl": "https://cdn.example.com/icons/urban-tour-planner.png",
    "documentationUrl": "https://docs.example.com/urban-tour-planner",
    "webAppUrl": "https://demo.example.com/urban-tour-planner",
    "provider": {
        "organization": "ACPs工作组",
        "department": "",
        "url": "https://ioa.pub",
        "license": "京ICP备2025124884号-4",
    },
    "securitySchemes": {
        "mtls": {"type": "mutualTLS", "description": "智能体间mTLS双向认证，确保高安全级别通信"},
        "oidc": {
            "type": "openIdConnect",
            "description": "基于OpenID Connect的用户身份认证",
            "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration",
        },
    },
    "endPoints": [
        {"url": "https://api.example.com/urban-tour-planner/rpc", "transport": "JSONRPC", "security": [{"mtls": []}]}
    ],
    "capabilities": {"streaming": True, "notification": True, "messageQueue": ["rabbitmq:3.*"]},
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["text/plain", "application/json", "text/markdown"],
    "skills": [
        {
            "id": "beijing-urban-tour.sight-recommender",
            "name": "景点推荐",
            "description": "根据用户需求推荐北京城六区内的旅游景点，提供详细的景点信息、开放时间、门票价格和文化背景介绍。",
            "version": "1.2.0",
            "tags": ["旅游", "景点推荐", "北京", "城区", "文化体验", "历史古迹"],
            "examples": ["推荐几个适合带小孩的北京城区景点", "我想了解故宫周边有什么文化景点"],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["text/plain", "application/json", "text/markdown"],
        },
        {
            "id": "beijing-urban-tour.transport-advisor",
            "name": "交通指南",
            "description": "提供北京城区内景点间的最优交通路线建议。",
            "version": "1.0.0",
            "tags": ["交通导航", "路线规划"],
            "examples": ["从故宫到颐和园怎么走最方便"],
        },
    ],
}

# ACPs-spec-ACS 4.2 全国范围旅游助手示例（节选；原文 messageQueue 同样不在枚举内）
SPEC_EXAMPLE_NATIONAL: Dict[str, Any] = {
    "aic": "1.2.156.3088.1.34C2.478BDE.3GF546.1.0RBK",
    "active": True,
    "lastModifiedTime": "2025-04-10T09:45:00+08:00",
    "protocolVersion": "02.02",
    "name": "全国范围旅游助手",
    "description": "提供中国全国范围内的旅游信息服务和行程规划。",
    "version": "2.2.0",
    "iconUrl": "https://cdn.example.com/icons/national-tour-guide.png",
    "provider": {"organization": "ACPs工作组", "department": "", "url": "https://ioa.pub",
                 "license": "京ICP备2025124884号-4"},
    "securitySchemes": {
        "mtls": {"type": "mutualTLS", "description": "智能体间高安全级别通信认证"},
        "oidc": {
            "type": "openIdConnect",
            "description": "用户身份认证和授权管理",
            "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration",
        },
    },
    "endPoints": [
        {"url": "https://api.example.com/national-tour-guide/v2", "transport": "HTTP_JSON",
         "security": [{"oidc": ["openid", "profile", "tour:coordinate"]}]},
        {"url": "https://api.example.com/national-tour-guide/rpc", "transport": "JSONRPC",
         "security": [{"mtls": []}]},
    ],
    "capabilities": {"streaming": True, "notification": True, "messageQueue": ["kafka:>=2.8 <4.0", "mqtt:5.*"]},
    "defaultInputModes": ["text/plain", "application/json", "image/jpeg"],
    "defaultOutputModes": ["text/plain", "application/json", "text/markdown", "application/xml"],
    "skills": [
        {
            "id": "national-tour:agent-coordination",
            "name": "智能体协调",
            "description": "作为Leader角色，协调和调度其他地区专业旅游智能体。",
            "version": "1.5.0",
            "tags": ["智能体协调", "任务调度"],
            "inputModes": ["application/json"],
            "outputModes": ["application/json", "application/xml"],
        }
    ],
}

MINIMAL_SPEC: Dict[str, Any] = {
    "aic": "1.2.156.3088.1.34C2.478BDF.3GF546.1.0SEN",
    "active": False,
    "lastModifiedTime": "2025-03-15T16:30:00+08:00",
    "protocolVersion": "02.02",
    "name": "n",
    "description": "d",
    "version": "1.0.0",
    "provider": {},
    "securitySchemes": {},
    "capabilities": {"streaming": False, "notification": False},
}


def with_message_queue(spec: Dict[str, Any], queues: List[str]) -> Dict[str, Any]:
    fixed = copy.deepcopy(spec)
    fixed["capabilities"]["messageQueue"] = queues
    return fixed


def dump(model: BaseModel) -> Any:
    return model.model_dump(by_alias=True, exclude_none=True, mode="json")


def errors_of(exc: ValidationError) -> List[Dict[str, Any]]:
    return [{"loc": list(err["loc"]), "type": err["type"], "msg": err["msg"]} for err in exc.errors()]


def model_case(model: str, name: str, data: Any, *, compare_locs: bool = True) -> Dict[str, Any]:
    """compare_locs=False：错误位置含 pydantic 的 Union 成员标签，Node 侧只比对「是否抛错」。"""
    validator = MODELS[model]
    case: Dict[str, Any] = {"model": model, "name": name, "input": data}
    try:
        if isinstance(validator, TypeAdapter):
            instance = validator.validate_python(data)
        else:
            instance = validator.model_validate(data)
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


def gen_exports() -> None:
    write_fixture(PKG, "exports", {
        "all": list(acs.__all__),
        "mqProtocolVersion": {member.name: member.value for member in MQProtocolVersion},
    })


def gen_models() -> None:
    cases: List[Dict[str, Any]] = []
    add = cases.append

    # ── AgentProvider ──
    add(model_case("AgentProvider", "empty provider defaults countryCode", {}))
    add(model_case("AgentProvider", "explicit null countryCode is dropped", {"countryCode": None}))
    add(model_case("AgentProvider", "full provider", {
        "countryCode": "US", "organization": "ACPs工作组", "department": "", "url": "https://ioa.pub",
        "license": "京ICP备2025124884号-4", "name": "张三", "email": "zhangsan@example.com",
    }))
    add(model_case("AgentProvider", "unknown keys are ignored", {"organization": "o", "phone": "123"}))
    add(model_case("AgentProvider", "email must be string", {"email": 1}))

    # ── AgentCapabilities ──
    add(model_case("AgentCapabilities", "defaults messageQueue", {"streaming": True, "notification": False}))
    add(model_case("AgentCapabilities", "all protocol versions", {
        "streaming": False, "notification": True, "messageQueue": [member.value for member in MQProtocolVersion],
    }))
    add(model_case("AgentCapabilities", "wildcard version is not in the enum",
                   {"streaming": True, "notification": True, "messageQueue": ["rabbitmq:3.*"]}))
    add(model_case("AgentCapabilities", "null messageQueue",
                   {"streaming": True, "notification": True, "messageQueue": None}))
    add(model_case("AgentCapabilities", "missing flags", {"messageQueue": []}))

    # ── 安全方案 ──
    add(model_case("MutualTLSSecurityScheme", "type only", {"type": "mutualTLS"}))
    add(model_case("MutualTLSSecurityScheme", "with legacy challenge url", {
        "type": "mutualTLS", "description": "双向TLS认证", "x-caChallengeBaseUrl": "https://ca.example.com/challenge/v1",
    }))
    add(model_case("MutualTLSSecurityScheme", "missing type", {"description": "d"}))
    add(model_case("MutualTLSSecurityScheme", "wrong type", {"type": "mtls"}))
    add(model_case("OpenIdConnectSecurityScheme", "oidc", {
        "type": "openIdConnect", "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration",
    }))
    add(model_case("OpenIdConnectSecurityScheme", "missing url", {"type": "openIdConnect"}))
    add(model_case("APIKeySecurityScheme", "api key", {"type": "apiKey", "name": "X-API-Key", "in": "header"}))
    add(model_case("APIKeySecurityScheme", "invalid location", {"type": "apiKey", "name": "k", "in": "body"}))
    add(model_case("HTTPAuthSecurityScheme", "bearer", {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}))
    add(model_case("HTTPAuthSecurityScheme", "missing scheme", {"type": "http"}))
    add(model_case("OAuth2Flow", "flow defaults scopes", {"tokenUrl": "https://auth.example.com/token"}))
    add(model_case("OAuth2Flow", "scope descriptions must be strings", {"scopes": {"read": 1}}))
    add(model_case("OAuth2Flows", "flows", {
        "implicit": {"authorizationUrl": "https://auth.example.com/authorize", "scopes": {"read": "读取"}},
        "password": None,
        "clientCredentials": {"tokenUrl": "https://auth.example.com/token"},
        "authorizationCode": {"authorizationUrl": "a", "tokenUrl": "t", "refreshUrl": "r", "scopes": {}},
    }))
    add(model_case("OAuth2SecurityScheme", "oauth2", {"type": "oauth2", "flows": {}}))
    add(model_case("OAuth2SecurityScheme", "missing flows", {"type": "oauth2"}))

    for scheme in [
        {"type": "mutualTLS"},
        {"type": "openIdConnect", "openIdConnectUrl": "u", "description": "d"},
        {"type": "apiKey", "name": "k", "in": "query"},
        {"type": "http", "scheme": "basic"},
        {"type": "oauth2", "flows": {"password": {"tokenUrl": "t"}}},
        {"type": "mutualTLS", "openIdConnectUrl": "ignored", "name": "ignored"},
    ]:
        add(model_case("SecurityScheme", f"union member {scheme['type']}", scheme))
    for name, scheme in [
        ("unknown type", {"type": "saml"}),
        ("missing type", {"openIdConnectUrl": "u"}),
        ("member validation fails", {"type": "apiKey", "name": "k"}),
        ("not an object", "mutualTLS"),
    ]:
        add(model_case("SecurityScheme", f"union rejects {name}", scheme, compare_locs=False))

    # ── AgentEndPoint ──
    add(model_case("AgentEndPoint", "endpoint with OR/AND security", {
        "url": "https://api.example.com/rpc", "transport": "JSONRPC",
        "security": [{"mtls": []}, {"oidc": ["openid", "profile"], "mtls": []}],
    }))
    add(model_case("AgentEndPoint", "endpoint without security", {"url": "u", "transport": "HTTP_JSON"}))
    add(model_case("AgentEndPoint", "security scopes must be lists", {"url": "u", "transport": "t", "security": [{"mtls": "x"}]}))
    add(model_case("AgentEndPoint", "missing transport", {"url": "u"}))

    # ── AgentSkill ──
    add(model_case("AgentSkill", "minimal skill defaults tags", {"id": "s", "name": "n", "description": "d", "version": "1"}))
    add(model_case("AgentSkill", "skill with null optionals", {
        "id": "s", "name": "n", "description": "d", "version": "1", "examples": None, "inputModes": None,
        "outputModes": None,
    }))
    add(model_case("AgentSkill", "spec skill", SPEC_EXAMPLE_URBAN["skills"][0]))
    add(model_case("AgentSkill", "missing id and version", {"name": "n", "description": "d"}))
    add(model_case("AgentSkill", "tags must be strings", {"id": "s", "name": "n", "description": "d", "version": "1",
                                                           "tags": ["a", 2]}))

    # ── AgentCapabilitySpec ──
    spec = "AgentCapabilitySpec"
    add(model_case(spec, "spec example 4.1 as published (wildcard messageQueue)", SPEC_EXAMPLE_URBAN))
    add(model_case(spec, "spec example 4.1 with enum messageQueue",
                   with_message_queue(SPEC_EXAMPLE_URBAN, ["rabbitmq:3.11"])))
    add(model_case(spec, "spec example 4.2 as published (range messageQueue)", SPEC_EXAMPLE_NATIONAL))
    add(model_case(spec, "spec example 4.2 with enum messageQueue",
                   with_message_queue(SPEC_EXAMPLE_NATIONAL, ["kafka:3.1", "mqtt:5.0"])))
    add(model_case(spec, "minimal spec gets defaults", MINIMAL_SPEC))
    add(model_case(spec, "entity fields keep nested null", {
        **MINIMAL_SPEC, "entityUserId": "user-1", "entityMeta": {"location": "Beijing", "tags": None},
    }))
    add(model_case(spec, "all security scheme types", {
        **MINIMAL_SPEC,
        "securitySchemes": {
            "mtls": {"type": "mutualTLS", "x-caChallengeBaseUrl": "https://ca.example.com/c"},
            "oidc": {"type": "openIdConnect", "openIdConnectUrl": "u"},
            "key": {"type": "apiKey", "name": "k", "in": "cookie"},
            "basic": {"type": "http", "scheme": "basic"},
            "oauth": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": "t", "scopes": {"a": "b"}}}},
        },
    }))
    add(model_case(spec, "unknown keys are ignored", {**MINIMAL_SPEC, "extensions": {"x": 1}, "registry": "r"}))
    add(model_case(spec, "explicit null optionals", {
        **MINIMAL_SPEC, "iconUrl": None, "documentationUrl": None, "webAppUrl": None, "entityUserId": None,
        "entityMeta": None, "provider": {"countryCode": None},
    }))
    add(model_case(spec, "empty object", {}))
    add(model_case(spec, "null endPoints", {**MINIMAL_SPEC, "endPoints": None}))
    add(model_case(spec, "invalid nested provider and skill", {
        **MINIMAL_SPEC, "provider": {"url": 1}, "skills": [{"id": "s"}],
    }))
    add(model_case(spec, "invalid security scheme member", {
        **MINIMAL_SPEC, "securitySchemes": {"bad": {"type": "saml"}},
    }, compare_locs=False))

    write_fixture(PKG, "models", cases)


def gen_divergences() -> None:
    cases = [
        model_case("AgentCapabilities", "lax: string and int booleans", {"streaming": "yes", "notification": 1}),
        model_case("AgentCapabilitySpec", "lax: string active", {**MINIMAL_SPEC, "active": "true"}),
    ]
    for case in cases:
        case["kind"] = "lax"
    by_name = [
        model_case("AgentSkill", "populate_by_name: input_modes",
                   {"id": "s", "name": "n", "description": "d", "version": "1", "input_modes": ["text/plain"]}),
        model_case("APIKeySecurityScheme", "populate_by_name: in_location",
                   {"type": "apiKey", "name": "k", "in_location": "header"}),
        model_case("MutualTLSSecurityScheme", "populate_by_name: x_ca_challenge_base_url",
                   {"type": "mutualTLS", "x_ca_challenge_base_url": "u"}),
    ]
    for case in by_name:
        case["kind"] = "populateByName"
    write_fixture(PKG, "divergences", cases + by_name)


def gen_methods() -> None:
    valid = with_message_queue(SPEC_EXAMPLE_URBAN, ["rabbitmq:3.11", "mqtt:5.0"])
    spec = AgentCapabilitySpec.from_dict(valid)
    serialization = {
        "input": valid,
        "toDict": spec.to_dict(),
        "toJson": spec.to_json(),
        "fromJsonOfToJson": AgentCapabilitySpec.from_json(spec.to_json()).to_dict(),
    }

    files_dir = FIXTURE_ROOT / PKG / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(valid, ensure_ascii=False, indent=2) + "\n"
    for filename, content in [
        ("agent.json", text),
        ("agent.txt", text),
        ("agent-upper.JSON", text),
        ("agent-invalid.json", json.dumps(SPEC_EXAMPLE_URBAN, ensure_ascii=False, indent=2) + "\n"),
        ("agent-broken.json", '{"aic": '),
    ]:
        (files_dir / filename).write_text(content, encoding="utf-8")

    # 路径相对包根目录书写，错误信息里的路径与 Node 侧替换后可直接比对。
    # 统一入口在同一进程里依次运行所有生成脚本，切换的工作目录用完即恢复。
    previous_cwd = os.getcwd()
    os.chdir(PACKAGE_ROOT)
    try:
        from_file_cases = _from_file_cases(files_dir.relative_to(PACKAGE_ROOT).as_posix())
    finally:
        os.chdir(previous_cwd)

    write_fixture(PKG, "methods", {"serialization": serialization, "fromFile": from_file_cases})


def _from_file_cases(rel: str) -> List[Dict[str, Any]]:
    from_file_cases = []
    for path in [
        f"{rel}/agent.json",
        f"{rel}/agent-upper.JSON",
        f"{rel}/agent.txt",
        f"{rel}/missing.json",
        f"{rel}/missing.txt",
        f"{rel}/agent-invalid.json",
        f"{rel}/agent-broken.json",
    ]:
        case: Dict[str, Any] = {"path": path}
        try:
            loaded = AgentCapabilitySpec.from_file(path)
        except FileNotFoundError as exc:
            case["error"] = {"kind": "FileNotFoundError", "message": str(exc)}
        except ValidationError as exc:
            invalid_json = any(err["type"] == "json_invalid" for err in exc.errors())
            case["error"] = {"kind": "InvalidJSON" if invalid_json else "ValidationError", "errors": errors_of(exc)}
        except ValueError as exc:
            case["error"] = {"kind": "ValueError", "message": str(exc)}
        else:
            case["result"] = loaded.to_dict()
        from_file_cases.append(case)
    return from_file_cases


def main() -> None:
    gen_exports()
    gen_models()
    gen_divergences()
    gen_methods()


if __name__ == "__main__":
    main()
