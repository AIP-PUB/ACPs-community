"""unified.py — admin mq 命令组入口（admin_mq_group）。"""

from __future__ import annotations

import json

import click

from acps_cli.mq.auth_probe_cmd import (
    probe_resource,
    probe_topic,
    probe_user,
    probe_vhost,
)
from acps_cli.mq.client import MqAuthClient
from acps_cli.mq.config import MqConfig
from acps_cli.mq.group_cmd import add_member, delete_group, kick_member, remove_member
from acps_cli.shared.flexible_group import FlexibleGroup
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
    """从 RootCliRuntime 的 toml_data 中加载 MqConfig。"""
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


def _probe_cert_args(cfg: MqConfig, cli_cert: str | None, cli_key: str | None) -> tuple[str, str]:
    """解析 probe 证书路径：CLI > probe_cert_file > 报错。

    Returns:
        (cert_file, key_file)

    Raises:
        click.ClickException: 无法确定证书来源时。
    """
    cert = cli_cert or cfg.probe_cert_file
    key = cli_key or cfg.probe_key_file
    if not cert or not key:
        raise click.ClickException(
            "未找到 probe 客户端证书。请通过 --cert-file / --key-file 指定，"
            "或在 [mq] 配置节设置 probe_cert_file / probe_key_file。"
        )
    return cert, key


def _build_probe_client(
    cfg: MqConfig,
    base_url: str,
    cli_cert: str | None,
    cli_key: str | None,
) -> MqAuthClient:
    cert, key = _probe_cert_args(cfg, cli_cert, cli_key)
    return MqAuthClient(
        base_url=base_url,
        cert_file=cert,
        key_file=key,
        ca_cert_file=cfg.ca_cert_file,
        timeout=cfg.timeout_seconds,
    )


def _probe_one(client: MqAuthClient, path: str) -> dict[str, str]:
    """探测单个端点，返回 {"status": "ok"|"error", "detail": "..."}。"""
    try:
        status, _body = client.get(path)
        if status == 200:
            return {"status": "ok", "detail": ""}
        return {"status": "error", "detail": f"HTTP {status}"}
    except click.ClickException as exc:
        return {"status": "error", "detail": str(exc.format_message())}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


# ─── health 命令 ──────────────────────────────────────────────────────────────


@click.command(name="health", help="检查 mq-auth-server Group API 和 Auth API 健康状态。")
@click.option("--cert-file", default=None, help="客户端证书 PEM 文件（覆盖配置）。")
@click.option("--key-file", default=None, help="客户端私钥 PEM 文件（覆盖配置）。")
@click.option("--json", "output_json", is_flag=True, help="以 JSON 格式输出结果。")
@click.pass_context
def health(ctx: click.Context, cert_file: str | None, key_file: str | None, output_json: bool) -> None:
    """同时探测 Group API（9007）和 Auth API（9008）的 /health 端点。

    任一端点不可达时以 exit 0 返回 status: error（health 的语义是报告状态，而非断言）。
    """
    cfg = _get_mq_config(ctx)

    # 构建两个探测客户端：共用同一套证书
    cert = cert_file or cfg.probe_cert_file
    key = key_file or cfg.probe_key_file

    def _probe(base_url: str) -> dict[str, str]:
        if not cert or not key:
            return {
                "status": "error",
                "detail": "未配置 probe 客户端证书（probe_cert_file / probe_key_file）",
            }
        try:
            client = MqAuthClient(
                base_url=base_url,
                cert_file=cert,
                key_file=key,
                ca_cert_file=cfg.ca_cert_file,
                timeout=cfg.timeout_seconds,
            )
            return _probe_one(client, "/health")
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    group_result = _probe(cfg.group_api_url)
    auth_result = _probe(cfg.auth_api_url)

    result = {"group_api": group_result, "auth_api": auth_result}

    if output_json:
        click.echo(json.dumps(result))
        return

    # 人类可读输出
    def _fmt(label: str, info: dict[str, str]) -> None:
        status = info["status"]
        detail = info.get("detail", "")
        symbol = "✓" if status == "ok" else "✗"
        msg = f"  {symbol} {label}: {status}"
        if detail:
            msg += f" ({detail})"
        click.echo(msg)

    click.echo("mq-auth-server health:")
    _fmt("Group API", group_result)
    _fmt("Auth API", auth_result)


# ─── group 命令组（来自 group_cmd.py） ───────────────────────────────────────


@click.group(cls=FlexibleGroup, name="group", help="管理 mq-auth-server 群组 ACL（Leader 专属）。")
@click.pass_context
def group_group(ctx: click.Context) -> None:
    # 仅包含子命令，顶层不执行操作
    pass


group_group.add_command(add_member)
group_group.add_command(remove_member)
group_group.add_command(delete_group)
group_group.add_command(kick_member)


# ─── auth-probe 命令组（来自 auth_probe_cmd.py） ──────────────────────────────


@click.group(cls=FlexibleGroup, name="auth-probe", help="探测 mq-auth-server Auth API 的授权决策。")
@click.pass_context
def auth_probe_group(ctx: click.Context) -> None:
    # 仅包含子命令，顶层不执行操作
    pass


auth_probe_group.add_command(probe_user)
auth_probe_group.add_command(probe_vhost)
auth_probe_group.add_command(probe_resource)
auth_probe_group.add_command(probe_topic)


# ─── admin_mq_group 顶层 Group ────────────────────────────────────────────────


@click.group(cls=FlexibleGroup, name="mq", help="mq-auth-server 管理命令（Group ACL 与 Auth API 探测）。")
@click.option("--group-api-url", default=None, help="覆盖 mq-auth-server Group API 地址。")
@click.option("--auth-api-url", default=None, help="覆盖 mq-auth-server Auth API 地址。")
@click.pass_context
def admin_mq_group(ctx: click.Context, group_api_url: str | None, auth_api_url: str | None) -> None:
    root_ctx = ctx.find_root()
    root_ctx.ensure_object(dict)
    root_obj = root_ctx.obj
    if not isinstance(root_obj, dict):
        raise click.ClickException("Unified CLI runtime is not initialized.")
    root_obj[MQ_CLI_OVERRIDES_KEY] = {
        "group_api_url": group_api_url,
        "auth_api_url": auth_api_url,
    }


admin_mq_group.add_command(health)
admin_mq_group.add_command(group_group)
admin_mq_group.add_command(auth_probe_group)
