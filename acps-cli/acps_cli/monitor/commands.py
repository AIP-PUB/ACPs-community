"""Monitor 查询 CLI 命令实现。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import click

from acps_cli.monitor.client import MonitorClient, MonitorClientError
from acps_cli.shared.auth_session import AuthSessionError, OidcAuthSessionManager
from acps_cli.shared.flexible_group import FlexibleGroup
from acps_cli.shared.unified_config import ServiceAuthConfig

SHORTCUT_FILTER_CONFLICT_MESSAGE = (
    "Shortcut filters only support request.filter with logic=and and no groups. Put the full filter in request JSON."
)


def echo_json(data: Any) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _print_auth_payload(data: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        echo_json(data)
        return
    for key, value in data.items():
        rendered = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        click.echo(f"{key}: {rendered}")


def _print_device_prompt(prompt: Any, *, as_json: bool) -> None:
    click.echo(
        f"Open this URL in a browser: {prompt.verification_uri_complete or prompt.verification_uri}",
        err=as_json,
    )
    click.echo(f"Enter this code if prompted: {prompt.user_code}", err=as_json)


def _load_json_object_from_text(value: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid {label} JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{label} JSON must be an object")
    return payload


def _load_json_object_from_file(file_path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid {label} JSON file: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{label} JSON file must contain an object")
    return payload


def load_json_source(
    *,
    request_json: str | None,
    request_file: Path | None,
    label: str = "request",
    text_option: str = "--request-json",
    file_option: str = "--request-file",
) -> dict[str, Any] | None:
    if request_json is not None and request_file is not None:
        raise click.ClickException(f"{text_option} and {file_option} cannot be used together")
    if request_json is not None:
        return _load_json_object_from_text(request_json, label)
    if request_file is not None:
        return _load_json_object_from_file(request_file, label)
    return None


def merge_time_range(payload: dict[str, Any], start: str | None, end: str | None) -> None:
    if start is None and end is None:
        return
    if start is None or end is None:
        raise click.ClickException("--start and --end must be used together")
    payload["timeRange"] = {"startAt": start, "endAt": end}


def merge_page(payload: dict[str, Any], limit: int | None, cursor: str | None) -> None:
    if limit is None and cursor is None:
        return
    existing = payload.get("page")
    if existing is None:
        page: dict[str, Any] = {}
    elif isinstance(existing, dict):
        page = dict(existing)
    else:
        raise click.ClickException("request.page must be an object when combining with --limit/--cursor")
    if limit is not None:
        page["limit"] = limit
    if cursor is not None:
        page["cursor"] = cursor
    payload["page"] = page


def append_filter_condition(payload: dict[str, Any], field: str, op: str, value: Any) -> None:
    if value is None:
        return
    existing = payload.get("filter")
    if existing is None:
        filter_payload: dict[str, Any] = {"logic": "and", "conditions": []}
        payload["filter"] = filter_payload
    elif isinstance(existing, dict):
        filter_payload = existing
    else:
        raise click.ClickException("request.filter must be an object when combining with shortcut filters")

    logic = filter_payload.get("logic")
    if logic is None:
        filter_payload["logic"] = "and"
    elif logic != "and":
        raise click.ClickException(SHORTCUT_FILTER_CONFLICT_MESSAGE)

    groups = filter_payload.get("groups")
    if groups not in (None, []):
        raise click.ClickException(SHORTCUT_FILTER_CONFLICT_MESSAGE)

    conditions = filter_payload.get("conditions")
    if conditions is None:
        conditions = []
        filter_payload["conditions"] = conditions
    if not isinstance(conditions, list):
        raise click.ClickException("request.filter.conditions must be an array when combining with shortcut filters")
    conditions.append({"field": field, "op": op, "value": value})


def require_request_payload(payload: dict[str, Any] | None, command_name: str) -> dict[str, Any]:
    if payload is None:
        raise click.ClickException(f"{command_name} requires --request-json or --request-file")
    return payload


def _copy_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return deepcopy(payload) if payload is not None else {}


def _build_client(ctx_obj: dict[str, Any]) -> MonitorClient:
    auth_config = _monitor_auth_config(ctx_obj)
    return MonitorClient(
        base_url=str(ctx_obj["server_base_url"]),
        api_prefix=str(ctx_obj["api_prefix"]),
        timeout=float(ctx_obj["timeout_seconds"]),
        auth_mode=auth_config.mode,
        auth_session=_build_auth_manager(ctx_obj),
    )


def _monitor_auth_config(ctx_obj: dict[str, Any]) -> ServiceAuthConfig:
    auth_config = ctx_obj.get("auth_config")
    if not isinstance(auth_config, ServiceAuthConfig):
        raise click.ClickException("Monitor auth configuration is not initialized.")
    return auth_config


def _build_auth_manager(ctx_obj: dict[str, Any]) -> OidcAuthSessionManager | None:
    auth_config = _monitor_auth_config(ctx_obj)
    if auth_config.mode != "oidc":
        return None
    return OidcAuthSessionManager(auth_config)


def _raise_monitor_click_error(exc: MonitorClientError) -> None:
    if exc.json_body is not None:
        click.echo(json.dumps(exc.json_body, ensure_ascii=False, indent=2), err=True)
    elif exc.raw_body:
        click.echo(exc.raw_body, err=True)
    raise click.ClickException(str(exc))


def _raise_auth_click_error(exc: Exception) -> None:
    raise click.ClickException(str(exc)) from exc


def _run_get(ctx_obj: dict[str, Any], path: str, *, params: dict[str, Any] | None = None) -> None:
    client = _build_client(ctx_obj)
    try:
        result = client.get_api(path, params=params)
    except MonitorClientError as exc:
        _raise_monitor_click_error(exc)
    echo_json(result)


def _run_health(ctx_obj: dict[str, Any]) -> None:
    client = _build_client(ctx_obj)
    try:
        result = client.get_health()
    except MonitorClientError as exc:
        _raise_monitor_click_error(exc)
    echo_json(result)


def _run_post(ctx_obj: dict[str, Any], path: str, payload: dict[str, Any]) -> None:
    client = _build_client(ctx_obj)
    try:
        result = client.post_api(path, payload)
    except MonitorClientError as exc:
        _raise_monitor_click_error(exc)
    echo_json(result)


def _request_source_options(func: Any) -> Any:
    decorated = click.option(
        "--request-file",
        type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
        default=None,
        help="Path to a request JSON file.",
    )(func)
    return click.option(
        "--request-json",
        default=None,
        help="Inline request JSON object.",
    )(decorated)


def _page_options(func: Any) -> Any:
    decorated = click.option("--cursor", default=None, help="Page cursor.")(func)
    return click.option("--limit", type=click.IntRange(min=1), default=None, help="Maximum page size.")(decorated)


def _time_range_options(func: Any) -> Any:
    decorated = click.option("--end", default=None, help="timeRange.endAt override.")(func)
    return click.option("--start", default=None, help="timeRange.startAt override.")(decorated)


def _base_request_payload(request_json: str | None, request_file: Path | None) -> dict[str, Any] | None:
    return load_json_source(request_json=request_json, request_file=request_file)


def _quote_path_segment(value: str) -> str:
    return quote(value, safe="")


@click.command(help="Check monitor-server health.")
@click.pass_context
def status(ctx: click.Context) -> None:
    """检查 monitor-server 健康状态。"""
    _run_health(ctx.obj or {})


@click.group(cls=FlexibleGroup, name="auth", help="Manage monitor OIDC authentication.")
def auth_group() -> None:
    """Monitor auth 命令组。"""


@auth_group.command("login")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_login(ctx: click.Context, as_json: bool) -> None:
    auth_config = _monitor_auth_config(ctx.obj or {})
    if auth_config.mode != "oidc":
        raise click.ClickException('monitor auth is not enabled. Set [monitor.auth].mode = "oidc" first.')
    manager = _build_auth_manager(ctx.obj or {})
    assert manager is not None

    try:
        manager.login(on_prompt=lambda prompt: _print_device_prompt(prompt, as_json=as_json))
    except (AuthSessionError, MonitorClientError) as exc:
        _raise_auth_click_error(exc)
    payload = {"message": "Monitor login successful", **manager.whoami(), "authenticated": True}
    _print_auth_payload(payload, as_json=as_json)


@auth_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_status(ctx: click.Context, as_json: bool) -> None:
    auth_config = _monitor_auth_config(ctx.obj or {})
    if auth_config.mode != "oidc":
        _print_auth_payload(
            {
                "service": "monitor",
                "account_kind": "user",
                "auth_mode": "none",
                "authenticated": False,
            },
            as_json=as_json,
        )
        return
    manager = _build_auth_manager(ctx.obj or {})
    assert manager is not None
    try:
        payload = manager.status()
    except AuthSessionError as exc:
        _raise_auth_click_error(exc)
    _print_auth_payload(payload, as_json=as_json)


@auth_group.command("whoami")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_whoami(ctx: click.Context, as_json: bool) -> None:
    manager = _build_auth_manager(ctx.obj or {})
    if manager is None:
        raise click.ClickException('monitor auth is not enabled. Set [monitor.auth].mode = "oidc" first.')
    try:
        payload = manager.whoami()
    except AuthSessionError as exc:
        _raise_auth_click_error(exc)
    _print_auth_payload(payload, as_json=as_json)


@auth_group.command("refresh")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_refresh(ctx: click.Context, as_json: bool) -> None:
    manager = _build_auth_manager(ctx.obj or {})
    if manager is None:
        raise click.ClickException('monitor auth is not enabled. Set [monitor.auth].mode = "oidc" first.')
    try:
        manager.refresh()
        payload = {"message": "Monitor session refreshed", **manager.whoami()}
    except AuthSessionError as exc:
        _raise_auth_click_error(exc)
    _print_auth_payload(payload, as_json=as_json)


@auth_group.command("logout")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def auth_logout(ctx: click.Context, as_json: bool) -> None:
    manager = _build_auth_manager(ctx.obj or {})
    if manager is None:
        _print_auth_payload(
            {
                "service": "monitor",
                "account_kind": "user",
                "auth_mode": "none",
                "local_session_cleared": False,
                "revocation_attempted": False,
                "revoked": False,
            },
            as_json=as_json,
        )
        return
    try:
        payload = manager.logout()
    except AuthSessionError as exc:
        _raise_auth_click_error(exc)
    _print_auth_payload(payload, as_json=as_json)


@click.group(cls=FlexibleGroup, name="heartbeat", help="Query heartbeat read models.")
def heartbeat_group() -> None:
    """Heartbeat 命令组。"""


@heartbeat_group.command(name="summary", help="Show heartbeat summary.")
@click.pass_context
def heartbeat_summary(ctx: click.Context) -> None:
    """查询 Heartbeat 汇总信息。"""
    _run_get(ctx.obj or {}, "/heartbeat/summary")


@heartbeat_group.command(name="liveness", help="Get liveness for one AIC.")
@click.argument("aic")
@click.pass_context
def heartbeat_liveness(ctx: click.Context, aic: str) -> None:
    """查询单个 AIC 的 liveness。"""
    _run_get(ctx.obj or {}, f"/heartbeat/liveness/{_quote_path_segment(aic)}")


@heartbeat_group.command(name="query", help="Query heartbeat liveness snapshots.")
@click.option("--aic", default=None, help="Append aic eq filter.")
@_page_options
@_request_source_options
@click.pass_context
def heartbeat_query(
    ctx: click.Context,
    aic: str | None,
    limit: int | None,
    cursor: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """批量查询 Heartbeat liveness 快照。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and aic is None:
        raise click.ClickException("heartbeat query requires --aic when request payload is not provided")
    payload = _copy_payload(base_payload)
    merge_page(payload, limit, cursor)
    append_filter_condition(payload, "aic", "eq", aic)
    _run_post(ctx.obj or {}, "/heartbeat/liveness/query", payload)


@click.group(cls=FlexibleGroup, name="metrics", help="Query metrics read models.")
def metrics_group() -> None:
    """Metrics 命令组。"""


@metrics_group.command(name="snapshots", help="Query latest metric snapshots.")
@click.option("--aic", default=None, help="Append aic eq filter.")
@_page_options
@_request_source_options
@click.pass_context
def metrics_snapshots(
    ctx: click.Context,
    aic: str | None,
    limit: int | None,
    cursor: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询最新 Metrics 快照。"""
    payload = _copy_payload(_base_request_payload(request_json, request_file))
    merge_page(payload, limit, cursor)
    append_filter_condition(payload, "aic", "eq", aic)
    _run_post(ctx.obj or {}, "/metrics/snapshots/query", payload)


@metrics_group.command(name="series", help="Query metric time series.")
@click.option("--metric", default=None, help="Metric name override.")
@click.option("--aic", default=None, help="Append aic eq filter.")
@_time_range_options
@click.option("--step", default=None, help="Series step override.")
@_request_source_options
@click.pass_context
def metrics_series(
    ctx: click.Context,
    metric: str | None,
    aic: str | None,
    start: str | None,
    end: str | None,
    step: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询 Metrics 时序数据。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and (metric is None or start is None or end is None):
        raise click.ClickException(
            "metrics series requires --metric, --start, and --end when request payload is not provided"
        )
    payload = _copy_payload(base_payload)
    if metric is not None:
        payload["metric"] = metric
    merge_time_range(payload, start, end)
    if step is not None:
        payload["step"] = step
    append_filter_condition(payload, "aic", "eq", aic)
    _run_post(ctx.obj or {}, "/metrics/series/query", payload)


@metrics_group.command(name="rankings", help="Query metric rankings.")
@click.option("--metric", default=None, help="Metric name override.")
@_time_range_options
@click.option("--top-n", "top_n", type=click.IntRange(min=1), default=None, help="TopN override.")
@_request_source_options
@click.pass_context
def metrics_rankings(
    ctx: click.Context,
    metric: str | None,
    start: str | None,
    end: str | None,
    top_n: int | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询 Metrics 排行结果。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and (metric is None or start is None or end is None):
        raise click.ClickException(
            "metrics rankings requires --metric, --start, and --end when request payload is not provided"
        )
    payload = _copy_payload(base_payload)
    if metric is not None:
        payload["metric"] = metric
    merge_time_range(payload, start, end)
    if top_n is not None:
        payload["topN"] = top_n
    _run_post(ctx.obj or {}, "/metrics/rankings/query", payload)


@click.group(cls=FlexibleGroup, name="access", help="Query access read models.")
def access_group() -> None:
    """Access 命令组。"""


@access_group.command(name="events", help="Query access events.")
@click.option("--aic", default=None, help="Append aic eq filter.")
@click.option("--trace-id", default=None, help="Append traceId eq filter.")
@_time_range_options
@_page_options
@_request_source_options
@click.pass_context
def access_events(
    ctx: click.Context,
    aic: str | None,
    trace_id: str | None,
    start: str | None,
    end: str | None,
    limit: int | None,
    cursor: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询 Access 原始事件。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and (start is None or end is None):
        raise click.ClickException("access events requires --start and --end when request payload is not provided")
    payload = _copy_payload(base_payload)
    merge_time_range(payload, start, end)
    merge_page(payload, limit, cursor)
    append_filter_condition(payload, "aic", "eq", aic)
    append_filter_condition(payload, "traceId", "eq", trace_id)
    _run_post(ctx.obj or {}, "/access/events/query", payload)


def _post_payload_only(
    ctx_obj: dict[str, Any],
    path: str,
    *,
    request_json: str | None,
    request_file: Path | None,
    command_name: str,
) -> None:
    payload = require_request_payload(_base_request_payload(request_json, request_file), command_name)
    _run_post(ctx_obj, path, payload)


@access_group.command(name="operations", help="Query access operation summaries.")
@_request_source_options
@click.pass_context
def access_operations(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询 Access 操作聚合摘要。"""
    _post_payload_only(
        ctx.obj or {},
        "/access/operations/query",
        request_json=request_json,
        request_file=request_file,
        command_name="access operations",
    )


@access_group.command(name="traces", help="Query access trace summaries.")
@_request_source_options
@click.pass_context
def access_traces(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询 Access Trace 摘要列表。"""
    _post_payload_only(
        ctx.obj or {},
        "/access/traces/query",
        request_json=request_json,
        request_file=request_file,
        command_name="access traces",
    )


@access_group.command(name="trace", help="Get one access trace.")
@click.argument("trace_id")
@click.option("--include-events", is_flag=True, help="Include access events in the trace response.")
@click.pass_context
def access_trace(ctx: click.Context, trace_id: str, include_events: bool) -> None:
    """查询单条 Access Trace。"""
    _run_get(
        ctx.obj or {},
        f"/access/traces/{_quote_path_segment(trace_id)}",
        params={"include_events": include_events},
    )


@access_group.command(name="slow", help="Query slow access requests.")
@_request_source_options
@click.pass_context
def access_slow(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询 Access 慢请求分析结果。"""
    _post_payload_only(
        ctx.obj or {},
        "/access/slow-requests/top",
        request_json=request_json,
        request_file=request_file,
        command_name="access slow",
    )


@access_group.command(name="errors", help="Query access error attribution.")
@_request_source_options
@click.pass_context
def access_errors(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询 Access 错误归因结果。"""
    _post_payload_only(
        ctx.obj or {},
        "/access/errors/attribution",
        request_json=request_json,
        request_file=request_file,
        command_name="access errors",
    )


@access_group.command(name="topology", help="Query access topology.")
@_request_source_options
@click.pass_context
def access_topology(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询 Access 拓扑结果。"""
    _post_payload_only(
        ctx.obj or {},
        "/access/topology/query",
        request_json=request_json,
        request_file=request_file,
        command_name="access topology",
    )


@click.group(cls=FlexibleGroup, name="message", help="Query message read models.")
def message_group() -> None:
    """Message 命令组。"""


@message_group.command(name="events", help="Query message events.")
@click.option("--message-id", default=None, help="Append messageId eq filter.")
@click.option("--trace-id", default=None, help="Append traceId eq filter.")
@_time_range_options
@_page_options
@_request_source_options
@click.pass_context
def message_events(
    ctx: click.Context,
    message_id: str | None,
    trace_id: str | None,
    start: str | None,
    end: str | None,
    limit: int | None,
    cursor: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询 Message 原始事件。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and (start is None or end is None):
        raise click.ClickException("message events requires --start and --end when request payload is not provided")
    payload = _copy_payload(base_payload)
    merge_time_range(payload, start, end)
    merge_page(payload, limit, cursor)
    append_filter_condition(payload, "messageId", "eq", message_id)
    append_filter_condition(payload, "traceId", "eq", trace_id)
    _run_post(ctx.obj or {}, "/message/events/query", payload)


@message_group.command(name="lifecycles", help="Query message lifecycles.")
@_request_source_options
@click.pass_context
def message_lifecycles(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询 Message 生命周期聚合。"""
    _post_payload_only(
        ctx.obj or {},
        "/message/lifecycles/query",
        request_json=request_json,
        request_file=request_file,
        command_name="message lifecycles",
    )


@message_group.command(name="lifecycle", help="Get one message lifecycle.")
@click.argument("message_id")
@click.option("--system", default=None, help="system query parameter.")
@click.option("--destination-name", default=None, help="destinationName query parameter.")
@click.option("--destination-kind", default=None, help="destinationKind query parameter.")
@click.option("--virtual-host", default=None, help="virtualHost query parameter.")
@click.pass_context
def message_lifecycle(
    ctx: click.Context,
    message_id: str,
    system: str | None,
    destination_name: str | None,
    destination_kind: str | None,
    virtual_host: str | None,
) -> None:
    """查询单条 Message 生命周期详情。"""
    params = {
        "system": system,
        "destinationName": destination_name,
        "destinationKind": destination_kind,
        "virtualHost": virtual_host,
    }
    filtered_params = {key: value for key, value in params.items() if value is not None}
    _run_get(
        ctx.obj or {},
        f"/message/lifecycles/{_quote_path_segment(message_id)}",
        params=filtered_params or None,
    )


@message_group.command(name="deadletters", help="Query deadletter messages.")
@_request_source_options
@click.pass_context
def message_deadletters(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询死信 Message。"""
    _post_payload_only(
        ctx.obj or {},
        "/message/deadletters/query",
        request_json=request_json,
        request_file=request_file,
        command_name="message deadletters",
    )


@message_group.command(name="destinations", help="Query destination states.")
@_request_source_options
@click.pass_context
def message_destinations(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询目的地状态快照。"""
    _post_payload_only(
        ctx.obj or {},
        "/message/destinations/query",
        request_json=request_json,
        request_file=request_file,
        command_name="message destinations",
    )


@message_group.command(name="throughput", help="Query destination throughput.")
@_request_source_options
@click.pass_context
def message_throughput(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """查询目的地吞吐时序。"""
    _post_payload_only(
        ctx.obj or {},
        "/message/destinations/throughput",
        request_json=request_json,
        request_file=request_file,
        command_name="message throughput",
    )


@click.group(cls=FlexibleGroup, name="system", help="Query system read models.")
def system_group() -> None:
    """System 命令组。"""


@system_group.command(name="events", help="Query system events.")
@click.option("--aic", default=None, help="Append aic eq filter.")
@click.option("--correlation-id", default=None, help="Append correlationId eq filter.")
@click.option("--severity-min", type=int, default=None, help="Append severityNumber gte filter.")
@_time_range_options
@_page_options
@_request_source_options
@click.pass_context
def system_events(
    ctx: click.Context,
    aic: str | None,
    correlation_id: str | None,
    severity_min: int | None,
    start: str | None,
    end: str | None,
    limit: int | None,
    cursor: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询 System 日志事件。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and (start is None or end is None):
        raise click.ClickException("system events requires --start and --end when request payload is not provided")
    payload = _copy_payload(base_payload)
    merge_time_range(payload, start, end)
    merge_page(payload, limit, cursor)
    append_filter_condition(payload, "aic", "eq", aic)
    append_filter_condition(payload, "correlationId", "eq", correlation_id)
    append_filter_condition(payload, "severityNumber", "gte", severity_min)
    _run_post(ctx.obj or {}, "/system/events/query", payload)


@click.group(cls=FlexibleGroup, name="audit", help="Query audit read models and integrity tasks.")
def audit_group() -> None:
    """Audit 命令组。"""


@audit_group.command(name="records", help="Query audit records.")
@click.option("--aic", default=None, help="Append aic eq filter.")
@click.option("--keyword", default=None, help="Top-level keyword override.")
@_time_range_options
@_page_options
@_request_source_options
@click.pass_context
def audit_records(
    ctx: click.Context,
    aic: str | None,
    keyword: str | None,
    start: str | None,
    end: str | None,
    limit: int | None,
    cursor: str | None,
    request_json: str | None,
    request_file: Path | None,
) -> None:
    """查询 Audit 记录列表。"""
    base_payload = _base_request_payload(request_json, request_file)
    if base_payload is None and (start is None or end is None):
        raise click.ClickException("audit records requires --start and --end when request payload is not provided")
    payload = _copy_payload(base_payload)
    merge_time_range(payload, start, end)
    merge_page(payload, limit, cursor)
    if keyword is not None:
        payload["keyword"] = keyword
    append_filter_condition(payload, "aic", "eq", aic)
    _run_post(ctx.obj or {}, "/audit/records/query", payload)


@audit_group.command(name="record", help="Get one audit record.")
@click.argument("audit_id")
@click.pass_context
def audit_record(ctx: click.Context, audit_id: str) -> None:
    """查询单条 Audit 记录。"""
    _run_get(ctx.obj or {}, f"/audit/records/{_quote_path_segment(audit_id)}")


@audit_group.command(name="anchors", help="Query latest audit anchors.")
@click.option("--chain-id", default=None, help="chain_id query parameter.")
@click.pass_context
def audit_anchors(ctx: click.Context, chain_id: str | None) -> None:
    """查询最新 Audit 链锚点。"""
    params = {"chain_id": chain_id} if chain_id is not None else None
    _run_get(ctx.obj or {}, "/audit/anchors/latest", params=params)


@audit_group.command(name="verify", help="Submit audit integrity verification.")
@_request_source_options
@click.pass_context
def audit_verify(ctx: click.Context, request_json: str | None, request_file: Path | None) -> None:
    """提交 Audit 完整性校验任务。"""
    _post_payload_only(
        ctx.obj or {},
        "/audit/integrity/verify",
        request_json=request_json,
        request_file=request_file,
        command_name="audit verify",
    )


@audit_group.command(name="verify-task", help="Get one audit integrity verification task.")
@click.argument("task_id")
@click.pass_context
def audit_verify_task(ctx: click.Context, task_id: str) -> None:
    """查询单个 Audit 完整性校验任务。"""
    _run_get(ctx.obj or {}, f"/audit/integrity/verify/{_quote_path_segment(task_id)}")
