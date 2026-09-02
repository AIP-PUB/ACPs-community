import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import click
import httpx
from cryptography import x509 as crypto_x509
from cryptography.x509 import ocsp

from acps_cli.shared.config import load_toml_config
from acps_cli.shared.flexible_group import FlexibleGroup

from .acme import (
    AcmeClient,
    AcmeError,
    normalize_acme_object,
    normalize_runtime_url,
)
from .config import CliOverrides, Config
from .keys import generate_csr, generate_private_key, load_private_key, private_key_type_name, save_private_key
from .utils import ensure_directory, setup_logging

logger = logging.getLogger(__name__)

# 退出码常量（ATR-CA-Client.md §5.8）
EXIT_OK = 0
EXIT_GENERAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_NETWORK_ERROR = 3
EXIT_AUTH_ERROR = 4
EXIT_CERT_ERROR = 5
EXIT_FILE_ERROR = 6

MAX_POLL_ATTEMPTS = 30
POLL_INTERVAL_SECONDS = 2


def _new_acme_client(cfg: Config, account_key: Any | None) -> AcmeClient:
    """创建带管理 token 配置的 ACME 客户端。"""
    return AcmeClient(
        cfg.ca_server_atr_base_url,
        account_key,
        admin_api_token=cfg.admin_api_token,
    )


class AccountKeyResolution(NamedTuple):
    canonical_path: str
    load_path: str
    exists: bool
    source: str


def _load_eab_file(file_path: str | os.PathLike[str], expected_aic: str | None = None) -> dict[str, Any]:
    with open(file_path, encoding="utf-8") as file:
        try:
            payload = json.load(file)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Invalid EAB JSON file: {exc}") from exc

    if not isinstance(payload, dict):
        raise click.ClickException("EAB JSON must be an object")

    if not isinstance(payload.get("keyId"), str) or not isinstance(payload.get("macKey"), str):
        raise click.ClickException("EAB JSON must contain string fields: keyId, macKey")

    payload_aic = payload.get("aic")
    if expected_aic is not None:
        if not isinstance(payload_aic, str) or not payload_aic.strip():
            raise click.ClickException("EAB JSON must contain a non-empty string field: aic")
        if payload_aic.strip() != expected_aic:
            raise click.ClickException(f"EAB AIC mismatch: expected '{expected_aic}', got '{payload_aic.strip()}'.")

    return payload


def _resolve_account_key_path(
    cfg: Config,
    aic: str,
    *,
    allow_legacy_materialization: bool = False,
) -> AccountKeyResolution:
    """返回规范 account key 路径，并标记是否需要延迟迁移旧布局。"""
    canonical_path = cfg.account_key_path_for(aic)
    if os.path.exists(canonical_path):
        return AccountKeyResolution(
            canonical_path=canonical_path,
            load_path=canonical_path,
            exists=True,
            source="canonical",
        )

    cert_allowed_names = {f"{aic}.pem"}
    trust_bundle_path = Path(cfg.trust_bundle_path)
    certs_dir = Path(cfg.certs_dir)
    if trust_bundle_path.parent.resolve() == certs_dir.resolve():
        cert_allowed_names.add(trust_bundle_path.name)

    other_aic_artifacts: list[str] = []
    directory_patterns = [
        (Path(cfg.private_keys_dir), ".key", {f"{aic}.key"}),
        (Path(cfg.csr_dir), ".csr", {f"{aic}.csr"}),
        (certs_dir, ".pem", cert_allowed_names),
    ]
    for directory, suffix, allowed_names in directory_patterns:
        if not directory.is_dir():
            continue
        for entry_path in sorted(directory.iterdir()):
            entry = entry_path.name
            if not entry.endswith(suffix):
                continue
            if entry in allowed_names:
                continue
            other_aic_artifacts.append(str(entry_path))

    canonical_path_name = Path(canonical_path).name
    account_keys_dir = Path(cfg.account_keys_dir)
    existing_canonical_paths = (
        sorted(
            path.name
            for path in account_keys_dir.iterdir()
            if path.name.endswith(".account.key") and path.name != canonical_path_name
        )
        if account_keys_dir.is_dir()
        else []
    )

    for legacy_path in cfg.legacy_account_key_paths():
        if not os.path.exists(legacy_path):
            continue
        if not allow_legacy_materialization:
            raise click.ClickException(
                "Legacy shared account.key can only be materialized during cert issue after EAB AIC validation. "
                "Create or roll over a dedicated account key for this AIC before continuing."
            )
        if existing_canonical_paths or other_aic_artifacts:
            raise click.ClickException(
                "Legacy shared account.key cannot be auto-migrated once this workspace contains artifacts for another "
                "AIC. Create or roll over a dedicated account key for this AIC before continuing."
            )
        return AccountKeyResolution(
            canonical_path=canonical_path,
            load_path=legacy_path,
            exists=True,
            source="legacy",
        )

    return AccountKeyResolution(
        canonical_path=canonical_path,
        load_path=canonical_path,
        exists=False,
        source="missing",
    )


def _ensure_legacy_account_key_is_unbound(client: AcmeClient) -> None:
    try:
        client.new_account(only_return_existing=True)
    except AcmeError as exc:
        if exc.status_code == 404:
            return
        raise

    raise click.ClickException(
        "Legacy shared account.key is already bound to an existing ACME account. "
        "Generate or roll over a dedicated account key for this AIC before continuing."
    )


def _materialize_legacy_account_key(resolution: AccountKeyResolution) -> None:
    ensure_directory(os.path.dirname(resolution.canonical_path))
    shutil.copy2(resolution.load_path, resolution.canonical_path)
    logger.info(f"Copied legacy shared account key from {resolution.load_path} to {resolution.canonical_path}")


def _register_account(client, eab_credential, prefer_existing):
    if prefer_existing:
        try:
            return client.new_account(only_return_existing=True)
        except AcmeError as exc:
            if exc.status_code != 404:
                raise

    return client.new_account(eab_credential=eab_credential)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "name"):
        return value.name
    return str(value)


def _emit_json(payload: Any) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def _exit_with_error(message: str, exit_code: int = EXIT_GENERAL_ERROR) -> None:
    logger.error(message)
    raise SystemExit(exit_code)


def _extract_pem_certificates(pem_data: bytes) -> list[bytes]:
    begin_marker = b"-----BEGIN CERTIFICATE-----"
    end_marker = b"-----END CERTIFICATE-----"
    certificates: list[bytes] = []
    start = 0

    while True:
        begin = pem_data.find(begin_marker, start)
        if begin == -1:
            break
        end = pem_data.find(end_marker, begin)
        if end == -1:
            break
        end += len(end_marker)
        certificates.append(pem_data[begin:end] + b"\n")
        start = end

    return certificates


def _load_certificate_from_path(cert_path: str, label: str = "certificate") -> tuple[crypto_x509.Certificate, bytes]:
    try:
        with open(cert_path, "rb") as file:
            cert_pem = file.read()
    except OSError as exc:
        raise click.ClickException(f"Failed to read {label} file {cert_path}: {exc}") from exc

    try:
        cert = crypto_x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        raise click.ClickException(f"Invalid PEM certificate in {cert_path}: {exc}") from exc

    return cert, cert_pem


def _resolve_local_certificate(
    cfg: Config, aic: str | None, cert_path: str | None
) -> tuple[str, crypto_x509.Certificate, bytes]:
    resolved_cert_path = cert_path or (os.path.join(cfg.certs_dir, f"{aic}.pem") if aic else None)
    if not resolved_cert_path:
        raise click.ClickException("Either --aic or --cert must be provided")
    cert, cert_pem = _load_certificate_from_path(resolved_cert_path)
    return resolved_cert_path, cert, cert_pem


def _resolve_issuer_pem(certificate: crypto_x509.Certificate, issuer_path: str) -> bytes:
    try:
        with open(issuer_path, "rb") as file:
            issuer_bundle = file.read()
    except OSError as exc:
        raise click.ClickException(f"Failed to read issuer bundle {issuer_path}: {exc}") from exc

    pem_certificates = _extract_pem_certificates(issuer_bundle)
    if not pem_certificates:
        raise click.ClickException(f"No PEM certificates found in issuer bundle {issuer_path}")

    for issuer_pem in pem_certificates:
        issuer_cert = crypto_x509.load_pem_x509_certificate(issuer_pem)
        if issuer_cert.subject == certificate.issuer:
            return issuer_pem

    raise click.ClickException(
        f"Could not find issuer certificate for subject '{certificate.issuer.rfc4514_string()}' in {issuer_path}"
    )


def _get_response_datetime(response: Any, utc_attr: str, legacy_attr: str) -> str | None:
    timestamp = getattr(response, utc_attr, None)
    if timestamp is None:
        timestamp = getattr(response, legacy_attr, None)
    return timestamp.isoformat() if timestamp else None


def _ocsp_response_to_dict(response: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "responseStatus": response.response_status.name,
        "certificateStatus": None,
        "serialNumber": None,
        "thisUpdate": _get_response_datetime(response, "this_update_utc", "this_update"),
        "nextUpdate": _get_response_datetime(response, "next_update_utc", "next_update"),
        "revocationTime": _get_response_datetime(response, "revocation_time_utc", "revocation_time"),
        "revocationReason": None,
    }

    certificate_status = getattr(response, "certificate_status", None)
    if certificate_status is not None:
        payload["certificateStatus"] = certificate_status.name

    serial_number = getattr(response, "serial_number", None)
    if serial_number is not None:
        payload["serialNumber"] = format(serial_number, "X")

    revocation_reason = getattr(response, "revocation_reason", None)
    if revocation_reason is not None:
        payload["revocationReason"] = getattr(revocation_reason, "name", str(revocation_reason))

    return payload


def _resolve_serial_number(
    cfg: Config,
    serial_number: str | None,
    aic: str | None,
    cert_path: str | None,
) -> str:
    if serial_number:
        return serial_number

    _, cert, _ = _resolve_local_certificate(cfg, aic, cert_path)
    return format(cert.serial_number, "X")


def _run_ocsp_check(
    cfg: Config,
    client: AcmeClient,
    aic: str | None,
    cert_path: str | None,
    issuer: str | None,
    request_method: str,
) -> dict[str, Any]:
    resolved_cert_path, certificate, cert_pem = _resolve_local_certificate(cfg, aic, cert_path)
    issuer_path = issuer or cfg.trust_bundle_path
    issuer_pem = _resolve_issuer_pem(certificate, issuer_path)

    logger.debug("Checking OCSP status...")
    logger.debug(f"Certificate file: {resolved_cert_path}")
    logger.debug(f"Issuer file: {issuer_path}")

    response = client.check_ocsp(cert_pem, issuer_pem, method=request_method)
    payload = _ocsp_response_to_dict(response)
    payload["certPath"] = resolved_cert_path
    payload["issuerPath"] = issuer_path
    return payload


def _print_ocsp_summary(payload: dict[str, Any]) -> None:
    click.echo(f"OCSP response status: {payload['responseStatus']}")
    if payload["certificateStatus"]:
        click.echo(f"Certificate status: {payload['certificateStatus']}")
    if payload["serialNumber"]:
        click.echo(f"Serial number: {payload['serialNumber']}")
    if payload["thisUpdate"]:
        click.echo(f"This update: {payload['thisUpdate']}")
    if payload["nextUpdate"]:
        click.echo(f"Next update: {payload['nextUpdate']}")
    if payload["revocationTime"]:
        click.echo(f"Revocation time: {payload['revocationTime']}")
    if payload["revocationReason"]:
        click.echo(f"Revocation reason: {payload['revocationReason']}")


@click.group(cls=FlexibleGroup)
@click.option(
    "--config",
    "-c",
    default=None,
    help="Path to acps-cli.toml config file.",
)
@click.option("--server-url", default=None, help="Override CA server base URL")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.pass_context
def main(ctx, config, server_url, verbose):
    setup_logging(verbose)
    ctx.ensure_object(dict)
    toml_data, config_file_path = load_toml_config(config)
    ctx.obj["config"] = Config(
        toml_data.get("ca", {}),
        overrides=CliOverrides(server_base_url=server_url),
        config_file_path=str(config_file_path) if config_file_path else None,
    )
    logger.debug("CA CLI initialized")


def _run_new_cert(
    ctx: click.Context,
    aic: str,
    eab_file: str,
    usage: str,
    key_type: str,
    reuse_key: bool,
    key_path: str | None,
    cert_path: str | None,
    trust_bundle_path: str | None,
    *,
    allow_legacy_materialization: bool,
) -> None:
    cfg = ctx.obj["config"]
    eab_credential = _load_eab_file(eab_file, expected_aic=aic)

    # Resolve output paths (command-line overrides config defaults)
    agent_key_path = key_path or os.path.join(cfg.private_keys_dir, f"{aic}.key")
    final_cert_path = cert_path or os.path.join(cfg.certs_dir, f"{aic}.pem")
    final_trust_bundle_path = trust_bundle_path or cfg.trust_bundle_path
    csr_path = os.path.join(cfg.csr_dir, f"{aic}.csr")

    # Ensure directories exist
    for p in [agent_key_path, csr_path, final_cert_path, final_trust_bundle_path]:
        ensure_directory(os.path.dirname(p))

    # 1. Load or Generate Account Key
    account_key_resolution = _resolve_account_key_path(
        cfg,
        aic,
        allow_legacy_materialization=allow_legacy_materialization,
    )
    account_key_path = account_key_resolution.canonical_path
    if account_key_resolution.exists:
        logger.info(f"Loading account key from {account_key_resolution.load_path}")
        account_key = load_private_key(account_key_resolution.load_path)
    else:
        logger.info(f"Generating new account key ({key_type}) at {account_key_path}")
        account_key = generate_private_key(key_type)
        save_private_key(account_key, account_key_path)

    # 2. Generate Agent Key (only reuse if --reuse-key is set)
    if reuse_key and os.path.exists(agent_key_path):
        logger.info(f"Loading existing agent key from {agent_key_path}")
        agent_key = load_private_key(agent_key_path)
    else:
        logger.info(f"Generating new agent key ({key_type}) at {agent_key_path}")
        agent_key = generate_private_key(key_type)
        save_private_key(agent_key, agent_key_path)

    # 3. Generate CSR
    logger.info(f"Generating CSR for {aic}")
    logger.debug(f"CSR output path: {csr_path}")
    csr_obj = generate_csr(agent_key, aic, csr_path)
    from cryptography.hazmat.primitives import serialization

    csr_pem = csr_obj.public_bytes(serialization.Encoding.PEM)

    # 4. Initialize ACME Client
    client = _new_acme_client(cfg, account_key)

    try:
        if account_key_resolution.source == "legacy":
            _ensure_legacy_account_key_is_unbound(client)
            _materialize_legacy_account_key(account_key_resolution)

        # Create or retrieve Account
        logger.info("Registering ACME account...")
        _register_account(
            client,
            eab_credential=eab_credential,
            prefer_existing=account_key_resolution.source == "canonical",
        )

        # Create Order (with usage in identifier)
        logger.info(f"Creating certificate order for {aic} (usage={usage})...")
        order = client.new_order(aic, usage=usage)

        # Finalize Order
        logger.info("Finalizing order...")
        order = client.finalize_order(order["finalize"], csr_pem)

        poll_count = 0
        while order["status"] in ["processing", "pending"]:
            poll_count += 1
            if poll_count > MAX_POLL_ATTEMPTS:
                raise click.ClickException(
                    f"Order polling timed out after {MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS}s"
                )
            logger.debug(f"Order status: {order['status']}, polling ({poll_count}/{MAX_POLL_ATTEMPTS})...")
            time.sleep(POLL_INTERVAL_SECONDS)
            resp = client._post(order["url"], None)  # POST-as-GET to poll order
            order = normalize_acme_object(resp.json())
            order["url"] = normalize_runtime_url(resp.headers.get("Location", order.get("url")))

            if order["status"] == "valid":
                break
            if order["status"] == "invalid":
                raise click.ClickException(f"Order failed: {order.get('error')}")

        # Download Certificate
        if order["status"] == "valid":
            logger.info("Downloading certificate...")
            cert_pem = client.get_certificate(order["certificate"])

            with open(final_cert_path, "wb") as f:
                f.write(cert_pem)
            logger.info(f"Certificate saved to {final_cert_path}")

            # Also update trust bundle
            ctx.invoke(update_trust_bundle, output=final_trust_bundle_path)

    except AcmeError as e:
        logger.error(f"ACME error: {e}")
        if e.detail:
            logger.debug(f"Error detail: {e.detail}")
        ctx.exit(_acme_error_to_exit_code(e))


@main.command()
@click.option("--aic", "-a", required=True, help="Agent Identity Code")
@click.option(
    "--eab-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to EAB credential JSON file",
)
@click.option(
    "--usage",
    "-u",
    required=True,
    type=click.Choice(["clientAuth", "serverAuth"]),
    help="Certificate EKU usage: clientAuth or serverAuth",
)
@click.option(
    "--key-type",
    "-k",
    default="ed25519",
    type=click.Choice(["ec", "rsa", "ed25519"]),
    help="Key type",
)
@click.option(
    "--reuse-key",
    is_flag=True,
    default=False,
    help="Reuse existing agent private key if present",
)
@click.option(
    "--key-path",
    type=click.Path(dir_okay=False),
    help="Output path for agent private key",
)
@click.option(
    "--cert-path",
    type=click.Path(dir_okay=False),
    help="Output path for certificate chain",
)
@click.option(
    "--trust-bundle-path",
    type=click.Path(dir_okay=False),
    help="Output path for trust bundle",
)
@click.pass_context
def new_cert(
    ctx,
    aic,
    eab_file,
    usage,
    key_type,
    reuse_key,
    key_path,
    cert_path,
    trust_bundle_path,
):
    """Request a new certificate for an Agent."""
    _run_new_cert(
        ctx,
        aic,
        eab_file,
        usage,
        key_type,
        reuse_key,
        key_path,
        cert_path,
        trust_bundle_path,
        allow_legacy_materialization=True,
    )


@main.command()
@click.option("--aic", "-a", required=True, help="Agent Identity Code")
@click.option(
    "--eab-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to EAB credential JSON file",
)
@click.option(
    "--usage",
    "-u",
    required=True,
    type=click.Choice(["clientAuth", "serverAuth"]),
    help="Certificate EKU usage: clientAuth or serverAuth",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    default=False,
    help="Force renewal even if certificate is not near expiry",
)
@click.option(
    "--key-path",
    type=click.Path(dir_okay=False),
    help="Output path for agent private key",
)
@click.option(
    "--cert-path",
    type=click.Path(dir_okay=False),
    help="Output path for certificate chain",
)
@click.option(
    "--trust-bundle-path",
    type=click.Path(dir_okay=False),
    help="Output path for trust bundle",
)
@click.pass_context
def renew_cert(ctx, aic, eab_file, usage, force, key_path, cert_path, trust_bundle_path):
    """Renew an existing certificate."""
    cfg = ctx.obj["config"]
    existing_cert_path = cert_path or os.path.join(cfg.certs_dir, f"{aic}.pem")

    # Check certificate expiry unless --force is set
    if not force and os.path.exists(existing_cert_path):
        try:
            with open(existing_cert_path, "rb") as f:
                cert = crypto_x509.load_pem_x509_certificate(f.read())
            days_remaining = (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
            if days_remaining > 30:
                raise click.ClickException(
                    f"Certificate still valid for {days_remaining} days. Use --force to renew anyway."
                )
            logger.info(f"Certificate expires in {days_remaining} days, proceeding with renewal")
        except click.ClickException:
            raise
        except Exception as e:
            logger.warning(f"Could not check existing certificate: {e}")

    agent_key_path = key_path or os.path.join(cfg.private_keys_dir, f"{aic}.key")
    renew_key_type = "ed25519"
    if os.path.exists(agent_key_path):
        try:
            renew_key_type = private_key_type_name(load_private_key(agent_key_path))
        except Exception as e:
            logger.warning(f"Could not infer existing key type from {agent_key_path}: {e}")

    _run_new_cert(
        ctx,
        aic,
        eab_file,
        usage,
        renew_key_type,
        True,
        key_path,
        cert_path,
        trust_bundle_path,
        allow_legacy_materialization=False,
    )


@main.command()
@click.option("--aic", "-a", required=True, help="Agent Identity Code")
@click.option(
    "--new-key",
    "-n",
    type=click.Path(dir_okay=False),
    help="Path to a pre-generated key file or the destination for an auto-generated key",
)
@click.option(
    "--key-type",
    "-k",
    default="ed25519",
    type=click.Choice(["ec", "rsa", "ed25519"]),
    help="Key type when auto-generating a new key",
)
@click.option(
    "--backup/--no-backup",
    default=True,
    help="Backup the current account key before replacing it",
)
@click.pass_context
def key_rollover(ctx, aic, new_key, key_type, backup):
    """Rotate the ACME account key pair."""
    cfg = ctx.obj["config"]
    account_key_resolution = _resolve_account_key_path(cfg, aic)
    account_key_path = account_key_resolution.canonical_path
    account_key_exists = account_key_resolution.exists

    if not account_key_exists:
        raise click.ClickException(f"Account key not found at {account_key_path}. Create an account first.")

    old_key = load_private_key(account_key_path)
    client = _new_acme_client(cfg, old_key)

    logger.info("Retrieving existing ACME account...")
    try:
        client.new_account(only_return_existing=True)
    except AcmeError as exc:
        raise click.ClickException(f"Failed to retrieve account: {exc}") from exc

    new_key_obj = None
    new_key_output_path = None

    if new_key and os.path.exists(new_key):
        if os.path.samefile(new_key, account_key_path):
            raise click.ClickException(
                "The --new-key path points to the current account key. Provide a different file."
            )
        logger.info(f"Loading pre-generated key from {new_key}")
        new_key_obj = load_private_key(new_key)
        new_key_output_path = new_key
    else:
        logger.info(f"Generating new account key ({key_type})")
        new_key_obj = generate_private_key(key_type)
        new_key_output_path = new_key or None
        if new_key_output_path:
            logger.debug(f"New key output path: {new_key_output_path}")

    logger.info("Requesting key rollover from CA server...")
    try:
        client.key_change(new_key_obj)
    except AcmeError as exc:
        raise click.ClickException(f"Key rollover failed: {exc}") from exc

    backup_path = None
    if backup:
        timestamp = time.strftime("%Y%m%d%H%M%S")
        backup_path = f"{account_key_path}.bak-{timestamp}"
        shutil.copy2(account_key_path, backup_path)
        logger.info(f"Old key backed up to {backup_path}")

    save_private_key(new_key_obj, account_key_path)
    logger.info(f"Account key updated at {account_key_path}")

    if new_key_output_path:
        if not os.path.exists(new_key_output_path):
            save_private_key(new_key_obj, new_key_output_path)
            logger.debug(f"New key also saved to {new_key_output_path}")
        elif os.path.samefile(new_key_output_path, account_key_path):
            # Already saved when updating account key path
            pass
        else:
            logger.debug("Reused provided key file")

    if backup_path:
        logger.info("Old key backup retained. Remove it manually if not needed.")

    logger.info("Key rollover completed successfully")


@main.command()
@click.option("--aic", "-a", required=True, help="Agent Identity Code")
@click.option("--reason", "-r", default="unspecified", help="Revocation reason")
@click.pass_context
def revoke_cert(ctx, aic, reason):
    """Revoke a certificate."""
    cfg = ctx.obj["config"]
    cert_path = os.path.join(cfg.certs_dir, f"{aic}.pem")

    if not os.path.exists(cert_path):
        raise click.ClickException(f"Certificate not found at {cert_path}")

    # Load account key
    account_key_resolution = _resolve_account_key_path(cfg, aic)
    account_key_path = account_key_resolution.canonical_path
    account_key_exists = account_key_resolution.exists
    if not account_key_exists:
        raise click.ClickException(f"Account key not found at {account_key_path}")
    account_key = load_private_key(account_key_path)

    client = _new_acme_client(cfg, account_key)

    # Map reason string to code
    reasons = {
        "unspecified": 0,
        "keyCompromise": 1,
        "cACompromise": 2,
        "affiliationChanged": 3,
        "superseded": 4,
        "cessationOfOperation": 5,
    }
    reason_code = reasons.get(reason, 0)

    try:
        with open(cert_path, "rb") as f:
            cert_pem = f.read()

        logger.info(f"Revoking certificate for {aic}...")
        logger.debug(f"Certificate path: {cert_path}")
        logger.debug(f"Revocation reason: {reason} (code={reason_code})")
        client.revoke_cert(cert_pem, reason_code)
        logger.info("Certificate revoked successfully")
    except AcmeError as e:
        logger.error(f"Failed to revoke certificate: {e}")
        if e.detail:
            logger.debug(f"Error detail: {e.detail}")


@main.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False),
    help="Output path for trust bundle",
)
@click.pass_context
def update_trust_bundle(ctx, output):
    """Update the local trust bundle."""
    cfg = ctx.obj["config"]
    url = f"{cfg.ca_server_atr_base_url}/ca/trust-bundle"
    path = output or cfg.trust_bundle_path

    ensure_directory(os.path.dirname(path))

    logger.info("Downloading trust bundle...")
    logger.debug(f"Trust bundle URL: {url}")
    logger.debug(f"Trust bundle output path: {path}")
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        with open(path, "wb") as f:
            f.write(resp.content)
        logger.info(f"Trust bundle updated and saved to {path}")
    except httpx.HTTPError as e:
        logger.error(f"Failed to update trust bundle: {e}")


@main.command()
@click.option("--output", "-o", help="Output file path")
@click.option(
    "--format",
    "-f",
    "output_format",
    default="der",
    type=click.Choice(["der", "pem"]),
    help="CRL format",
)
@click.option(
    "--version",
    default=None,
    help="Historical CRL version to download (DER only)",
)
@click.pass_context
def download_crl(ctx, output, output_format, version):
    """Download the Certificate Revocation List (CRL)."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    if version and output_format != "der":
        raise click.ClickException("Historical CRL download only supports DER format")

    try:
        logger.info(f"Downloading CRL ({output_format} format){f' version {version}' if version else ''}...")
        crl_content = client.download_crl(output_format=output_format, version=version)

        if output:
            out_path = output
        else:
            if version:
                out_path = os.path.join(cfg.certs_dir, f"ca-{version}.crl")
            else:
                ext = "crl" if output_format == "der" else "pem"
                out_path = os.path.join(cfg.certs_dir, f"ca.{ext}")

        ensure_directory(os.path.dirname(out_path))
        with open(out_path, "wb") as f:
            f.write(crl_content)
        logger.info(f"CRL saved to {out_path}")
    except AcmeError as exc:
        _exit_with_error(f"Failed to download CRL: {exc}", _acme_error_to_exit_code(exc))
    except OSError as exc:
        _exit_with_error(f"Failed to save CRL: {exc}", EXIT_FILE_ERROR)


@main.command("crl-info")
@click.pass_context
def crl_info(ctx):
    """Fetch CRL metadata from the CA server."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        _emit_json(client.get_crl_info())
    except AcmeError as exc:
        _exit_with_error(f"Failed to fetch CRL info: {exc}", _acme_error_to_exit_code(exc))


@main.command("crl-detail")
@click.pass_context
def crl_detail(ctx):
    """Fetch revoked certificate entries from the current CRL."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        _emit_json(client.get_crl_detail())
    except AcmeError as exc:
        _exit_with_error(f"Failed to fetch CRL detail: {exc}", _acme_error_to_exit_code(exc))


@main.command("crl-list")
@click.option(
    "--status",
    "crl_status",
    type=click.Choice(["current", "superseded", "expired"]),
    default=None,
    help="Filter CRLs by status",
)
@click.option("--page", default=1, type=int, show_default=True)
@click.option("--page-size", default=20, type=int, show_default=True)
@click.pass_context
def crl_list(ctx, crl_status, page, page_size):
    """List CRL history from the CA server."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        _emit_json(client.list_crls(status=crl_status, page=page, page_size=page_size))
    except AcmeError as exc:
        _exit_with_error(f"Failed to list CRLs: {exc}", _acme_error_to_exit_code(exc))


@main.command("refresh-crl")
@click.pass_context
def refresh_crl(ctx):
    """Refresh the current CRL on the CA server."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        _emit_json(client.refresh_crl())
    except AcmeError as exc:
        _exit_with_error(f"Failed to refresh CRL: {exc}", _acme_error_to_exit_code(exc))


@main.command("ocsp-responder-info")
@click.pass_context
def ocsp_responder_info(ctx):
    """Fetch OCSP responder information."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        _emit_json(client.get_ocsp_responder_info())
    except AcmeError as exc:
        _exit_with_error(
            f"Failed to fetch OCSP responder info: {exc}",
            _acme_error_to_exit_code(exc),
        )


@main.command("ocsp-stats")
@click.pass_context
def ocsp_stats(ctx):
    """Fetch OCSP service statistics."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        _emit_json(client.get_ocsp_statistics())
    except AcmeError as exc:
        _exit_with_error(
            f"Failed to fetch OCSP statistics: {exc}",
            _acme_error_to_exit_code(exc),
        )


@main.command("ocsp-cert-status")
@click.option("--serial-number", default=None, help="Certificate serial number")
@click.option("--aic", "aic", default=None, help="Agent Identity Code")
@click.option(
    "--cert-path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Path to certificate file used to derive the serial number",
)
@click.pass_context
def ocsp_cert_status(ctx, serial_number, aic, cert_path):
    """Fetch certificate status from the simplified OCSP endpoint."""
    cfg = ctx.obj["config"]
    client = _new_acme_client(cfg, None)

    try:
        resolved_serial = _resolve_serial_number(cfg, serial_number, aic, cert_path)
        payload = client.get_certificate_status(resolved_serial)
        payload.setdefault("serialNumber", resolved_serial)
        _emit_json(payload)
    except AcmeError as exc:
        _exit_with_error(
            f"Failed to fetch certificate OCSP status: {exc}",
            _acme_error_to_exit_code(exc),
        )


@main.command()
@click.option("--aic", "-a", default=None, help="Agent Identity Code")
@click.option(
    "--cert",
    "-c",
    "cert_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Certificate file path",
)
@click.option(
    "--issuer",
    "-i",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Issuer certificate or trust bundle file path",
)
@click.option(
    "--request-method",
    type=click.Choice(["post", "get"]),
    default="post",
    show_default=True,
    help="OCSP request method",
)
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
@click.pass_context
def check_ocsp(ctx, aic, cert_path, issuer, request_method, as_json):
    """Check certificate status via OCSP."""
    cfg = ctx.obj["config"]
    client = AcmeClient(cfg.ca_server_url, None, admin_api_token=cfg.admin_api_token)

    try:
        payload = _run_ocsp_check(cfg, client, aic, cert_path, issuer, request_method)

        if as_json:
            _emit_json(payload)
        else:
            _print_ocsp_summary(payload)
    except AcmeError as exc:
        _exit_with_error(f"OCSP check failed: {exc}", _acme_error_to_exit_code(exc))


@main.command()
@click.option("--aic", "-a", required=True, help="Agent Identity Code")
@click.option(
    "--cert-path",
    type=click.Path(dir_okay=False),
    help="Path to certificate file",
)
@click.option(
    "--check-ocsp/--no-check-ocsp",
    default=True,
    help="Check certificate status via OCSP (default: enabled)",
)
@click.pass_context
def status(ctx, aic, cert_path, check_ocsp):
    """Query certificate status for an Agent."""
    cfg = ctx.obj["config"]
    cert_file = cert_path or os.path.join(cfg.certs_dir, f"{aic}.pem")

    if not os.path.exists(cert_file):
        raise click.ClickException(f"Certificate not found at {cert_file}")

    try:
        with open(cert_file, "rb") as f:
            cert_pem = f.read()
        cert = crypto_x509.load_pem_x509_certificate(cert_pem)
    except Exception as e:
        raise click.ClickException(f"Failed to load certificate: {e}") from e

    # Display local certificate information
    subject = cert.subject.rfc4514_string()
    issuer_dn = cert.issuer.rfc4514_string()
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    now = datetime.now(timezone.utc)
    days_remaining = (not_after - now).days

    logger.info(f"Subject: {subject}")
    logger.info(f"Issuer: {issuer_dn}")
    logger.info(f"Not Before: {not_before.isoformat()}")
    logger.info(f"Not After: {not_after.isoformat()}")
    logger.info(f"Days remaining: {days_remaining}")

    if days_remaining < 0:
        logger.warning("Certificate status: EXPIRED")
    elif days_remaining <= 30:
        logger.warning(f"Certificate status: EXPIRING SOON ({days_remaining} days)")
    else:
        logger.info("Certificate status: VALID")

    # OCSP check (optional)
    if check_ocsp:
        trust_bundle_path = cfg.trust_bundle_path
        if not os.path.exists(trust_bundle_path):
            logger.warning(f"Trust bundle not found at {trust_bundle_path}, skipping OCSP check")
            return

        try:
            issuer_pem = _resolve_issuer_pem(cert, trust_bundle_path)

            client = _new_acme_client(cfg, None)
            ocsp_resp = client.check_ocsp(cert_pem, issuer_pem)

            if ocsp_resp.response_status == ocsp.OCSPResponseStatus.SUCCESSFUL:
                logger.info(f"OCSP status: {ocsp_resp.certificate_status}")
                if ocsp_resp.certificate_status == ocsp.OCSPCertStatus.REVOKED:
                    logger.warning(f"Certificate REVOKED at {ocsp_resp.revocation_time}")
            else:
                logger.warning(f"OCSP response status: {ocsp_resp.response_status}")
        except Exception as e:
            logger.warning(f"OCSP check failed: {e}")


def _acme_error_to_exit_code(error: AcmeError) -> int:
    """Map AcmeError to standardized exit code."""
    if error.status_code in (401, 403):
        return EXIT_AUTH_ERROR
    if error.status_code is not None:
        return EXIT_CERT_ERROR
    return EXIT_GENERAL_ERROR


if __name__ == "__main__":
    main()
