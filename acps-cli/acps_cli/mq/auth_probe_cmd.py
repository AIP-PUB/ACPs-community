"""auth_probe_cmd.py — admin mq auth-probe 子命令实现。"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from acps_cli.mq.client import MqAuthClient
from acps_cli.mq.config import MqConfig
from acps_cli.shared.runtime import get_root_runtime

# ─── 辅助函数 ─────────────────────────────────────────────────────────────────

MQ_CLI_OVERRIDES_KEY = "mq_cli_overrides"


def _get_cli_url_override(ctx: click.Context, key: str) -> str | None:
    root_obj = ctx.find_root().obj
    if not isinstance(root_obj, dict):
        return None
    overrides = root_obj.get(MQ_CLI_OVERRIDES_KEY)
    if not isinstance(overrides, dict):
        return None
    value = overrides.get(key)
    return value if isinstance(value, str) and value else None


def _get_mq_config(ctx: click.Context) -> MqConfig:
    """从 RootCliRuntime.toml_data 加载 MqConfig。"""
    runtime = get_root_runtime(ctx)
    mq_section = runtime.toml_data.get("mq", {})
    if not isinstance(mq_section, dict):
        mq_section = {}
    return MqConfig.from_toml(
        mq_section,
        runtime.config_dir,
        cli_group_api_url=_get_cli_url_override(ctx, "group_api_url"),
        cli_auth_api_url=_get_cli_url_override(ctx, "auth_api_url"),
    )


def _get_probe_client(
    cfg: MqConfig,
    cli_cert: str | None,
    cli_key: str | None,
) -> MqAuthClient:
    """构建 auth-probe 专用 mTLS 客户端（使用 probe 证书，任意合法 ACPs 证书均可）。

    优先级：CLI 参数 > 配置文件 probe_cert_file。
    """
    cert = cli_cert or cfg.probe_cert_file
    key = cli_key or cfg.probe_key_file
    if not cert or not key:
        raise click.ClickException(
            "未找到 probe 客户端证书。"
            "请通过 --cert-file / --key-file 指定，"
            "或在 [mq] 配置节设置 probe_cert_file / probe_key_file。"
        )
    return MqAuthClient(
        base_url=cfg.auth_api_url,
        cert_file=cert,
        key_file=key,
        ca_cert_file=cfg.ca_cert_file,
        timeout=cfg.timeout_seconds,
    )


def _output_probe_result(output_json: bool, result: str, extra_fields: dict[str, Any]) -> None:
    """输出 auth-probe 结果（allow/deny）。"""
    if output_json:
        click.echo(json.dumps({"result": result, **extra_fields}))
        return
    symbol = "✓ allow" if result == "allow" else "✗ deny"
    parts = ", ".join(f"{k}={v}" for k, v in extra_fields.items())
    click.echo(f"{symbol}  [{parts}]")


def _parse_auth_response(status: int, body: object, output_json: bool, extra_fields: dict[str, Any]) -> None:
    """解析 auth backend 响应（纯文本 allow/deny）。"""
    if status != 200:
        detail = body if isinstance(body, str) else json.dumps(body)
        if output_json:
            click.echo(json.dumps({"status": "error", "message": f"HTTP {status}: {detail}"}))
            sys.exit(1)
        raise click.ClickException(f"Auth API 返回非 200 状态（HTTP {status}）：{detail}")

    # RabbitMQ auth backend 返回纯文本 "allow" / "deny"
    result_text = body.strip() if isinstance(body, str) else str(body)
    if result_text not in ("allow", "deny"):
        result_text = "deny"  # 保守处理未知响应
    _output_probe_result(output_json, result_text, extra_fields)


# ─── auth-probe user ──────────────────────────────────────────────────────────


@click.command(name="user", help="探测 /auth/user 授权决策（POST username=<AIC>）。")
@click.option("--username", required=True, help="探测用户名（通常为 AIC）。")
@click.option("--cert-file", default=None, help="客户端证书 PEM（覆盖配置）。")
@click.option("--key-file", default=None, help="客户端私钥 PEM（覆盖配置）。")
@click.option("--json", "output_json", is_flag=True, help="以 JSON 格式输出结果。")
@click.pass_context
def probe_user(
    ctx: click.Context,
    username: str,
    cert_file: str | None,
    key_file: str | None,
    output_json: bool,
) -> None:
    """向 Auth API 发送 /auth/user 请求，查看授权决策。"""
    cfg = _get_mq_config(ctx)
    client = _get_probe_client(cfg, cert_file, key_file)
    form_data = {"username": username, "password": ""}  # nosec B105 - empty password is part of the probe request shape
    status, body = client.post_form("/auth/user", form_data)
    _parse_auth_response(status, body, output_json, {"username": username})


# ─── auth-probe vhost ─────────────────────────────────────────────────────────


@click.command(name="vhost", help="探测 /auth/vhost 授权决策（POST username+vhost）。")
@click.option("--username", required=True, help="用户名（AIC）。")
@click.option("--vhost", required=True, help="RabbitMQ vhost（通常为 'acps'）。")
@click.option("--cert-file", default=None, help="客户端证书 PEM（覆盖配置）。")
@click.option("--key-file", default=None, help="客户端私钥 PEM（覆盖配置）。")
@click.option("--json", "output_json", is_flag=True, help="以 JSON 格式输出结果。")
@click.pass_context
def probe_vhost(
    ctx: click.Context,
    username: str,
    vhost: str,
    cert_file: str | None,
    key_file: str | None,
    output_json: bool,
) -> None:
    """向 Auth API 发送 /auth/vhost 请求，查看 vhost 访问决策。"""
    cfg = _get_mq_config(ctx)
    client = _get_probe_client(cfg, cert_file, key_file)
    status, body = client.post_form("/auth/vhost", {"username": username, "vhost": vhost, "ip": ""})
    _parse_auth_response(status, body, output_json, {"username": username, "vhost": vhost})


# ─── auth-probe resource ──────────────────────────────────────────────────────


@click.command(
    name="resource",
    help="探测 /auth/resource 授权决策（POST exchange/queue 读写权限）。",
)
@click.option("--username", required=True, help="用户名（AIC）。")
@click.option("--vhost", required=True, help="RabbitMQ vhost。")
@click.option(
    "--resource",
    "resource_type",
    required=True,
    type=click.Choice(["exchange", "queue"]),
    help="资源类型。",
)
@click.option("--name", required=True, help="资源名称（exchange 或 queue 名）。")
@click.option(
    "--permission",
    required=True,
    type=click.Choice(["configure", "write", "read"]),
    help="所请求的权限类型。",
)
@click.option("--cert-file", default=None, help="客户端证书 PEM（覆盖配置）。")
@click.option("--key-file", default=None, help="客户端私钥 PEM（覆盖配置）。")
@click.option("--json", "output_json", is_flag=True, help="以 JSON 格式输出结果。")
@click.pass_context
def probe_resource(
    ctx: click.Context,
    username: str,
    vhost: str,
    resource_type: str,
    name: str,
    permission: str,
    cert_file: str | None,
    key_file: str | None,
    output_json: bool,
) -> None:
    """向 Auth API 发送 /auth/resource 请求，查看资源访问决策。"""
    cfg = _get_mq_config(ctx)
    client = _get_probe_client(cfg, cert_file, key_file)
    status, body = client.post_form(
        "/auth/resource",
        {
            "username": username,
            "vhost": vhost,
            "resource": resource_type,
            "name": name,
            "permission": permission,
        },
    )
    _parse_auth_response(
        status,
        body,
        output_json,
        {
            "username": username,
            "resource": resource_type,
            "name": name,
            "permission": permission,
        },
    )


# ─── auth-probe topic ─────────────────────────────────────────────────────────


@click.command(name="topic", help="探测 /auth/topic 授权决策（POST topic 路由权限）。")
@click.option("--username", required=True, help="用户名（AIC）。")
@click.option("--vhost", required=True, help="RabbitMQ vhost。")
@click.option(
    "--resource",
    "resource_type",
    default="topic",
    show_default=True,
    help="资源类型（通常为 topic）。",
)
@click.option("--name", required=True, help="Exchange 名称。")
@click.option(
    "--permission",
    required=True,
    type=click.Choice(["write", "read"]),
    help="所请求的权限类型。",
)
@click.option("--routing-key", required=True, help="消息路由键。")
@click.option("--cert-file", default=None, help="客户端证书 PEM（覆盖配置）。")
@click.option("--key-file", default=None, help="客户端私钥 PEM（覆盖配置）。")
@click.option("--json", "output_json", is_flag=True, help="以 JSON 格式输出结果。")
@click.pass_context
def probe_topic(
    ctx: click.Context,
    username: str,
    vhost: str,
    resource_type: str,
    name: str,
    permission: str,
    routing_key: str,
    cert_file: str | None,
    key_file: str | None,
    output_json: bool,
) -> None:
    """向 Auth API 发送 /auth/topic 请求，查看 topic 路由权限决策。"""
    cfg = _get_mq_config(ctx)
    client = _get_probe_client(cfg, cert_file, key_file)
    status, body = client.post_form(
        "/auth/topic",
        {
            "username": username,
            "vhost": vhost,
            "resource": resource_type,
            "name": name,
            "permission": permission,
            "routing_key": routing_key,
        },
    )
    _parse_auth_response(
        status,
        body,
        output_json,
        {
            "username": username,
            "resource": resource_type,
            "name": name,
            "routing_key": routing_key,
        },
    )
