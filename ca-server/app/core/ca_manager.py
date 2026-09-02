"""CA 证书管理器：负责加载 CA 根证书与私钥、签发证书、维护信任链"""

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

from .config import get_settings
from .crypto_utils import (
    PrivateKeyTypes,
    assert_private_key_matches_certificate,
    require_supported_public_key,
    verify_certificate_signature,
    x509_signature_algorithm_for,
)

logger = structlog.get_logger(__name__)


class CAManager:
    """CA 证书管理器"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.ca_cert: x509.Certificate | None = None
        self.ca_private_key: PrivateKeyTypes | None = None
        self.ca_chain_pems: list[x509.Certificate] = []
        self.trust_bundle_pem: str = ""
        self._load_ca_from_files(Path(self.settings.ca_cert_path), Path(self.settings.ca_key_path))
        self._load_chain(Path(self.settings.ca_chain_path))
        self._load_trust_bundle(Path(self.settings.trust_bundle_path))

    def _load_ca_from_files(self, cert_path: Path, key_path: Path) -> None:
        """从文件强制加载 CA 证书和私钥，文件不存在则抛出异常"""
        if not cert_path.exists():
            raise FileNotFoundError(
                f"CA 证书文件不存在：{cert_path}。请先执行 just prep certs 从共享开发 PKI 同步业务中间 CA 套件。"
            )
        if not key_path.exists():
            raise FileNotFoundError(
                f"CA 私钥文件不存在：{key_path}。请先执行 just prep certs 从共享开发 PKI 同步业务中间 CA 套件。"
            )
        try:
            # 加载证书
            with cert_path.open("rb") as f:
                cert_data = f.read()
                self.ca_cert = x509.load_pem_x509_certificate(cert_data, default_backend())

            # 加载私钥
            with key_path.open("rb") as f:
                key_data = f.read()
                loaded_private_key = serialization.load_pem_private_key(
                    key_data,
                    password=None,
                    backend=default_backend(),
                )
                if not isinstance(
                    loaded_private_key,
                    (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey),
                ):
                    raise TypeError("Unsupported CA private key type")
                self.ca_private_key = loaded_private_key
                assert_private_key_matches_certificate(self.ca_private_key, self.ca_cert)

            logger.info("已加载 CA 证书", cert_path=str(cert_path))
            valid_from = self.ca_cert.not_valid_before_utc
            valid_to = self.ca_cert.not_valid_after_utc
            logger.info(
                "CA 证书有效期",
                valid_from=str(valid_from),
                valid_to=str(valid_to),
                ca_key_type=type(self.ca_private_key).__name__,
            )

        except FileNotFoundError, TypeError:
            raise
        except Exception as e:
            logger.error("加载 CA 证书失败", error=str(e))
            raise

    def _load_chain(self, chain_path: Path) -> None:
        """从 ca-chain.pem 加载并校验证书链。

        校验规则：
        - 文件必须存在且可读
        - 内容须至少含一段有效 PEM
        - 第一段必须是 Intermediate CA（cert.subject != cert.issuer）
        - 第一段必须与 ca.crt（ca_cert）表示同一张证书
        """
        if not chain_path.exists():
            raise FileNotFoundError(
                f"CA 证书链文件不存在：{chain_path}。请先执行 just prep certs 从共享开发 PKI 同步完整证书套件。"
            )
        try:
            pem_data = chain_path.read_bytes()
        except OSError as e:
            raise RuntimeError(f"无法读取 CA 证书链文件：{chain_path}") from e

        # 解析所有 PEM 块
        certs: list[x509.Certificate] = []
        remaining = pem_data
        while b"-----BEGIN CERTIFICATE-----" in remaining:
            try:
                cert = x509.load_pem_x509_certificate(remaining, default_backend())
                certs.append(cert)
                # 截掉已解析的第一个证书块，继续解析剩余内容
                end_marker = b"-----END CERTIFICATE-----"
                idx = remaining.find(end_marker)
                remaining = remaining[idx + len(end_marker) :]
            except Exception:
                break

        if not certs:
            raise ValueError(f"ca-chain.pem 不含有效证书 PEM 块：{chain_path}")

        first_cert = certs[0]

        # 校验第一段是 Intermediate CA（subject != issuer）
        if first_cert.subject == first_cert.issuer:
            raise ValueError(f"ca-chain.pem 第一段是自签名证书（Root CA），应为 Intermediate CA：{chain_path}")

        # 校验第一段与 ca.crt 一致
        if self.ca_cert is None:
            raise RuntimeError("ca-chain.pem 校验前 ca.crt 尚未加载")
        if first_cert.fingerprint(hashes.SHA256()) != self.ca_cert.fingerprint(hashes.SHA256()):
            raise ValueError(f"ca-chain.pem 第一段与 ca.crt 不一致，请检查证书套件是否配套生成：{chain_path}")

        self.ca_chain_pems = certs
        logger.info(
            "已加载 CA 证书链",
            chain_path=str(chain_path),
            cert_count=len(certs),
        )

    def _load_trust_bundle(self, bundle_path: Path) -> None:
        """从 trust-bundle.pem 加载并校验信任包。

        校验规则：
        - 文件必须存在且可读
        - 内容须至少含一段有效 PEM 块
        """
        if not bundle_path.exists():
            raise FileNotFoundError(
                f"Trust Bundle 文件不存在：{bundle_path}。请先执行 just prep certs 从共享开发 PKI 同步完整证书套件。"
            )
        try:
            pem_text = bundle_path.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"无法读取 Trust Bundle 文件：{bundle_path}") from e

        if "-----BEGIN CERTIFICATE-----" not in pem_text:
            raise ValueError(f"trust-bundle.pem 不含有效 PEM 块：{bundle_path}")

        # 统计证书段数（用于日志）
        cert_count = pem_text.count("-----BEGIN CERTIFICATE-----")
        self.trust_bundle_pem = pem_text
        logger.info(
            "已加载 Trust Bundle",
            bundle_path=str(bundle_path),
            cert_count=cert_count,
        )

    def sign_certificate(
        self,
        csr: x509.CertificateSigningRequest,
        agent_ids: list[str],
        validity_days: int = 49,
        subject_components: dict[str, str] | None = None,
        dns_names: list[str] | None = None,
        ip_addresses: list[str] | None = None,
        usage: str = "clientAuth",
    ) -> str:
        """签发证书

        Args:
            csr: 证书签名请求
            agent_ids: Agent ID 列表（目前只支持单个 Agent）
            validity_days: 证书有效期（天数）
            subject_components: Agent 注册信息中的 Subject DN 组件
            dns_names: 额外的 DNS SAN 条目，来自 ACS certificate.altNames.dns
            ip_addresses: 额外的 IP SAN 条目，来自 ACS certificate.altNames.ip
            usage: 证书用途，可选 "clientAuth" 或 "serverAuth"（单一 EKU）
        """
        if not self.ca_cert or not self.ca_private_key:
            raise RuntimeError("CA 证书或私钥未加载")

        # 验证只支持单个Agent（根据用户要求，多Agent需分别签发）
        if len(agent_ids) != 1:
            raise ValueError("Currently only single agent certificates are supported")

        agent_id = agent_ids[0]

        # 验证CSR公钥算法
        self._validate_csr_public_key(csr)

        # 构造证书Subject DN（以Agent注册信息为准）
        subject = self._build_certificate_subject(agent_id, subject_components)

        # 创建证书
        cert_builder = x509.CertificateBuilder()

        # 设置主体（使用Agent注册信息构造，而非CSR中的信息）
        cert_builder = cert_builder.subject_name(subject)

        # 设置颁发者（CA）
        cert_builder = cert_builder.issuer_name(self.ca_cert.subject)

        # 设置公钥（从 CSR 获取）
        cert_builder = cert_builder.public_key(csr.public_key())

        # 设置序列号
        cert_builder = cert_builder.serial_number(x509.random_serial_number())

        # 设置有效期
        not_before = datetime.now(UTC)
        not_after = not_before + timedelta(days=validity_days)
        cert_builder = cert_builder.not_valid_before(not_before)
        cert_builder = cert_builder.not_valid_after(not_after)

        # 添加标准扩展
        cert_builder = self._add_standard_extensions(cert_builder, csr, usage)

        # 添加 Agent 特定的 SAN 扩展
        cert_builder = self._add_agent_san_extensions(cert_builder, agent_id, dns_names, ip_addresses)

        # 签名证书
        certificate = cert_builder.sign(
            private_key=self.ca_private_key,
            algorithm=x509_signature_algorithm_for(self.ca_private_key),
            backend=default_backend(),
        )

        # 返回 PEM 格式的证书
        return certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def _validate_csr_public_key(self, csr: x509.CertificateSigningRequest) -> None:
        """验证 CSR 中的公钥算法是否安全。

        允许算法：
        - RSA ≥ 2048 位
        - ECDSA P-256、P-384、P-521
        - Ed25519（RFC 8410）

        Args:
            csr: 证书签名请求

        Raises:
            ValueError: 公钥算法不在白名单内
        """
        public_key = csr.public_key()

        if isinstance(public_key, rsa.RSAPublicKey):
            key_size = public_key.key_size
            if key_size < 2048:
                raise ValueError(f"RSA key size {key_size} is too small. Minimum required: 2048 bits")
            if key_size > 4096:
                logger.warning("RSA 密钥长度过大，建议使用更小的密钥以提升性能", key_size=key_size)

        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            curve = public_key.curve
            allowed_curves = [ec.SECP256R1(), ec.SECP384R1(), ec.SECP521R1()]
            curve_allowed = any(isinstance(curve, type(c)) for c in allowed_curves)
            if not curve_allowed:
                raise ValueError(f"ECDSA curve {curve.name} is not allowed. Allowed curves: P-256, P-384, P-521")

        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            # Ed25519 无需额外参数校验（固定 255 位，RFC 8410）
            pass

        else:
            raise ValueError(
                f"Public key algorithm {type(public_key).__name__} is not supported. "
                f"Only RSA (≥2048 bits), ECDSA (P-256/P-384/P-521) and Ed25519 are allowed"
            )

    def _build_certificate_subject(self, agent_id: str, subject_components: dict[str, str] | None = None) -> x509.Name:
        """构造证书Subject DN"""
        # 基础Subject组件，CN必须是Agent对应的域名
        common_name = self.settings.build_agent_common_name(agent_id)
        name_attributes = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]

        # 添加Agent注册信息中的组织信息
        if subject_components:
            if "O" in subject_components:
                name_attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, subject_components["O"]))
            if "OU" in subject_components:
                name_attributes.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, subject_components["OU"]))
            if "C" in subject_components:
                name_attributes.append(x509.NameAttribute(NameOID.COUNTRY_NAME, subject_components["C"]))
            if "L" in subject_components:
                name_attributes.append(x509.NameAttribute(NameOID.LOCALITY_NAME, subject_components["L"]))
            if "ST" in subject_components:
                name_attributes.append(x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, subject_components["ST"]))

        return x509.Name(name_attributes)

    def _add_standard_extensions(
        self,
        cert_builder: x509.CertificateBuilder,
        csr: x509.CertificateSigningRequest,
        usage: str = "clientAuth",
    ) -> x509.CertificateBuilder:
        """添加标准证书扩展

        Args:
            cert_builder: 证书构建器
            csr: 证书签名请求
            usage: 证书用途，"clientAuth" 或 "serverAuth"（单一 EKU，v2.1.0 新规范）
        """
        # Subject Key Identifier
        cert_builder = cert_builder.add_extension(
            x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),
            critical=False,
        )

        # Authority Key Identifier
        if self.ca_cert is None:
            raise RuntimeError("CA certificate is not loaded")
        cert_builder = cert_builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(require_supported_public_key(self.ca_cert.public_key())),
            critical=False,
        )

        # Basic Constraints
        cert_builder = cert_builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )

        # Key Usage：Ed25519 是纯签名算法，依 RFC 8410 §3 只能设 digitalSignature，
        # 不得设 keyEncipherment；RSA/ECDSA 同时保留 keyEncipherment 以兼容 TLS 握手。
        csr_public_key = csr.public_key()
        is_ed25519 = isinstance(csr_public_key, ed25519.Ed25519PublicKey)
        cert_builder = cert_builder.add_extension(
            x509.KeyUsage(
                key_cert_sign=False,
                crl_sign=False,
                digital_signature=True,
                key_encipherment=not is_ed25519,
                data_encipherment=False,
                key_agreement=False,
                content_commitment=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )

        # Extended Key Usage（单一 EKU，由 usage 参数决定）
        if usage == "serverAuth":
            eku_oid = x509.oid.ExtendedKeyUsageOID.SERVER_AUTH
        else:
            eku_oid = x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH

        cert_builder = cert_builder.add_extension(
            x509.ExtendedKeyUsage([eku_oid]),
            critical=True,
        )

        cert_builder = cert_builder.add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(self.settings.ocsp_responder_url),
                    )
                ]
            ),
            critical=False,
        )

        return cert_builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(self.settings.crl_distribution_point_url)],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
        )

    def _add_agent_san_extensions(
        self,
        cert_builder: x509.CertificateBuilder,
        agent_id: str,
        dns_names: list[str] | None = None,
        ip_addresses: list[str] | None = None,
    ) -> x509.CertificateBuilder:
        """添加 Agent 特定的 SAN 扩展

        默认只生成 URI:acps://{AIC}。额外的 DNS/IP SAN 均来自 ACS certificate.altNames，
        不自动生成任何 DNS 记录。

        Args:
            cert_builder: 证书构建器
            agent_id: AIC
            dns_names: 额外 DNS SAN 条目（来自 ACS certificate.altNames.dns）
            ip_addresses: 额外 IP SAN 条目（来自 ACS certificate.altNames.ip）
        """
        san_list: list[x509.GeneralName] = []

        # 默认 SAN：URI:acps://{AIC}（协议标识符）
        san_list.append(x509.UniformResourceIdentifier(f"acps://{agent_id}"))

        # 来自 ACS certificate.altNames.dns 的 DNS SAN
        for dns_name in dns_names or []:
            san_list.append(x509.DNSName(dns_name))

        # 来自 ACS certificate.altNames.ip 的 IP SAN
        for ip_str in ip_addresses or []:
            try:
                san_list.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
            except ValueError:
                logger.warning("certificate.altNames.ip 中包含无效的 IP 地址", ip=ip_str)

        return cert_builder.add_extension(
            x509.SubjectAlternativeName(san_list),
            critical=False,
        )

    def get_ca_certificate_pem(self) -> str:
        """获取 CA 证书的 PEM 格式"""
        if not self.ca_cert:
            raise RuntimeError("CA 证书未加载")

        return self.ca_cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def get_issuer_chain_pem(self) -> str:
        """返回 ACME cert download 中应追加在叶子证书之后的 PEM 内容。

        在当前 Root CA -> Intermediate CA -> Leaf 两层链路里，只返回 Intermediate CA 证书
        （即 ca-chain.pem 的第一段，与 ca.crt 相同），不包含 Root CA。
        """
        if not self.ca_chain_pems:
            raise RuntimeError("CA 证书链未加载")
        # 第一段是 Intermediate CA，直接返回其 PEM（由 __init__ 保证与 ca.crt 一致）
        return self.ca_chain_pems[0].public_bytes(serialization.Encoding.PEM).decode("utf-8")

    def get_trust_bundle_pem(self) -> str:
        """返回 trust-bundle.pem 的原始 PEM 文本，供 /trust-bundle 端点直接返回"""
        if not self.trust_bundle_pem:
            raise RuntimeError("Trust Bundle 未加载")
        return self.trust_bundle_pem

    def get_ca_private_key_pem(self) -> str:
        """获取 CA 私钥的 PEM 格式"""
        if not self.ca_private_key:
            raise RuntimeError("CA 私钥未加载")

        return self.ca_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

    def verify_certificate_chain(self, cert_pem: str) -> bool:
        """验证证书链"""
        try:
            ca_cert = self.ca_cert
            if ca_cert is None or self.ca_private_key is None:
                return False

            # 加载证书
            cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"), default_backend())

            # 验证是否由当前 CA 签发
            if cert.issuer != ca_cert.subject:
                return False

            # 验证签名（这里简化处理，实际应该进行完整的证书链验证）
            try:
                verify_certificate_signature(cert, ca_cert)
                return True
            except Exception:
                return False

        except Exception:
            return False

    def _get_required_ca_private_key(self) -> PrivateKeyTypes:
        """获取已初始化的 CA 私钥"""
        if self.ca_private_key is None:
            raise RuntimeError("CA 私钥未加载")
        return self.ca_private_key


# 全局 CA 管理器实例
_ca_manager: CAManager | None = None


def get_ca_manager() -> CAManager:
    """获取 CA 管理器实例"""
    global _ca_manager
    if _ca_manager is None:
        _ca_manager = CAManager()
    return _ca_manager
