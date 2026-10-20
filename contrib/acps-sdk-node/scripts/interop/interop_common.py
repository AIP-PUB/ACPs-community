"""互通测试共用的常量、测试证书与结果记录。

证书在运行时生成到临时目录（进程结束即删除），不写进仓库。
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# 与两边 SDK 测试中使用的 AIC 一致（均通过 validate_aic_format）
LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6"

NEED_INPUT_MARK = "[need-input]"
INPUT_PROMPT = "请补充信息"
FILE_BYTES_B64 = base64.b64encode("hello, 世界\n".encode("utf-8")).decode("ascii")


def expected_products(task_id: str, text: str) -> list[dict[str, Any]]:
    """Partner 两种实现都按同一规则生成的产出物（agent.mts 里有同样的定义）。"""
    return [
        {
            "id": f"product-{task_id}",
            "name": "echo",
            "dataItems": [
                {"type": "text", "text": f"已处理：{text}"},
                {
                    "type": "data",
                    "data": {
                        "taskId": task_id,
                        "values": [1, 2.5, None, True, "✓"],
                        "nested": {"empty": None, "list": []},
                    },
                },
                {
                    "type": "file",
                    "name": "hello.txt",
                    "mimeType": "text/plain",
                    "bytes": FILE_BYTES_B64,
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# 测试 PKI（对应 Python tests/integration/_tls_test_utils.py 与 Node test/integration/tlsTestUtils.ts）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TlsFiles:
    """一个身份的证书文件（PEM 路径）。"""

    aic: str
    cert: Path
    key: Path
    ca: Path

    def spec(self, *, with_client_cert: bool = True) -> dict[str, str]:
        """传给 Node agent 的 TLS 描述。"""
        if not with_client_cert:
            return {"ca": str(self.ca)}
        return {"cert": str(self.cert), "key": str(self.key), "ca": str(self.ca)}


@dataclass(frozen=True)
class Pki:
    ca: Path
    leader: TlsFiles
    partner: TlsFiles
    other: TlsFiles
    # 由另一个（不受信任的）CA 签发、CN=LEADER_AIC 的证书，用来验证「未经校验的证书不能当身份用」
    rogue_leader: TlsFiles


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def _make_ca(common_name: str) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _issue_leaf(
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    aic: str,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, aic)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(f"acps://{aic}"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def create_pki(directory: Path) -> Pki:
    """在 directory 下生成测试 CA 与 Leader / Partner / Other 三个身份的叶子证书。"""
    ca_cert, ca_key = _make_ca("ACPs Interop Test CA")
    ca_path = directory / "ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    def issue(label: str, aic: str, issuer_cert=ca_cert, issuer_key=ca_key) -> TlsFiles:
        cert, key = _issue_leaf(issuer_cert, issuer_key, aic)
        cert_path = directory / f"{label}.pem"
        key_path = directory / f"{label}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _write_key(key_path, key)
        return TlsFiles(aic=aic, cert=cert_path, key=key_path, ca=ca_path)

    rogue_ca_cert, rogue_ca_key = _make_ca("Rogue CA")
    return Pki(
        ca=ca_path,
        leader=issue("leader", LEADER_AIC),
        partner=issue("partner", PARTNER_AIC),
        other=issue("other", OTHER_AIC),
        rogue_leader=issue("rogue-leader", LEADER_AIC, rogue_ca_cert, rogue_ca_key),
    )


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------


class CheckFailed(AssertionError):
    """单项断言失败。"""


@dataclass
class ScenarioResult:
    scenario: str
    direction: str
    status: str = "PASS"  # PASS / FAIL / XFAIL / SKIP
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    # 已知问题导致的预期失败：(断言名, 已知问题说明)
    xfails: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""


class Checker:
    """记录一个场景里的每条断言；默认第一条失败即中止该场景（后续断言依赖前面的状态），
    fatal=False 的断言只记录失败、继续往下执行。

    xfail 给出已知问题说明时，断言失败记为 XFAIL（不算失败）；若断言反而通过，记为失败并提示
    「已知问题已不复现，请去掉标记」（严格 xfail）。
    """

    def __init__(self, result: ScenarioResult, *, verbose: bool) -> None:
        self.result = result
        self.verbose = verbose

    def check(
        self,
        name: str,
        condition: bool,
        detail: Any = "",
        *,
        fatal: bool = True,
        xfail: str | None = None,
    ) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, default=str)
        if xfail is not None:
            if not condition:
                self.result.xfails.append((name, xfail))
                if self.verbose:
                    print(f"    [xfail] {name} :: known issue: {xfail[:200]}", flush=True)
                return
            condition, text = False, f"XPASS: known issue no longer reproduces, remove the xfail marker ({xfail[:120]})"
        self.result.checks.append((name, bool(condition), text if not condition else ""))
        if self.verbose:
            mark = "ok " if condition else "FAIL"
            print(f"    [{mark}] {name}" + ("" if condition else f" :: {text[:600]}"), flush=True)
        if not condition and fatal:
            raise CheckFailed(f"{name}: {text[:600]}")

    def equal(
        self, name: str, actual: Any, expected: Any, *, fatal: bool = True, xfail: str | None = None
    ) -> None:
        self.check(name, actual == expected, {"actual": actual, "expected": expected}, fatal=fatal, xfail=xfail)


_RPC_CODE = re.compile(r"RPC Error: (-?\d+)")


def rpc_error_code(error: dict[str, Any] | None) -> int | None:
    """从 SDK 抛出的错误文本（`RPC Error: -32009 - ...`）中取 JSON-RPC 错误码。"""
    if not error:
        return None
    match = _RPC_CODE.search(str(error.get("message", "")))
    return int(match.group(1)) if match else None
