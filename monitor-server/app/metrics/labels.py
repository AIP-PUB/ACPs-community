"""app/metrics/labels.py — Resource 标签派生与 TSDB 标签白名单（纯函数）。

实现设计 §2.4、§4.2：
- TSDB 标签空间的唯一允许集（C-METRIC-MODEL-1/2）
- 从 LogRecord.resource 派生低基数标签
- 防御性标签基数校验（写入侧最后一道防线）
"""

from __future__ import annotations

from typing import Any, Final

# ── 允许进入 TSDB 标签空间的字段白名单（C-METRIC-MODEL-1） ─────────────────────

RESOURCE_LABELS: Final = ("service_name", "service_namespace", "deployment_env")
"""从 LogRecord.resource 派生的资源标签（3 个低基数字段）。"""

SEMANTIC_LABELS: Final = ("window", "quantile")
"""语义标签：由展开逻辑（samples.py）按指标追加，不来自 resource。"""

ALLOWED_LABELS: Final = ("aic", *RESOURCE_LABELS, *SEMANTIC_LABELS)
"""TSDB 中允许的全部标签名集合（防止高基数字段污染 TSDB，C-METRIC-MODEL-1）。"""

_ALLOWED_LABELS_SET: frozenset[str] = frozenset(ALLOWED_LABELS)

# Resource 字段 → TSDB 标签名映射（§4.2 第 3 列）
_RESOURCE_KEY_MAP: Final[dict[str, str]] = {
    "service.name": "service_name",
    "service.namespace": "service_namespace",
    "deployment.environment.name": "deployment_env",
}


def derive_resource_labels(resource: dict[str, Any] | None) -> dict[str, str]:
    """从 LogRecord.resource 派生低基数 TSDB 标签。

    仅提取 service.name / service.namespace / deployment.environment.name 三个字段，
    对应 TSDB 标签 service_name / service_namespace / deployment_env（§4.2）。

    高基数字段（host.name / host.id / container ID / trace id 等）一律不派生，
    缺省字段直接省略（不写空串，C-METRIC-MODEL-2）。

    Args:
        resource: LogRecord.resource 字典，可为 None。

    Returns:
        dict[str, str]: 低基数标签字典，最多包含 3 个键。
    """
    if not resource:
        return {}
    result: dict[str, str] = {}
    for src_key, dst_key in _RESOURCE_KEY_MAP.items():
        value = resource.get(src_key)
        if isinstance(value, str) and value:
            result[dst_key] = value
    return result


def base_labels(aic: str, resource_labels: dict[str, str]) -> dict[str, str]:
    """构造所有 public series 的基础标签集（aic + 已出现的 resource 标签）。

    window / quantile 由 samples.py 展开逻辑按指标追加，不在此处包含。

    Args:
        aic: Agent Identity Code。
        resource_labels: derive_resource_labels 的返回值。

    Returns:
        dict[str, str]: 基础标签字典（aic 必含）。
    """
    return {"aic": aic, **resource_labels}


def assert_label_cardinality_safe(labels: dict[str, str]) -> None:
    """防御性校验：确保 labels 的全部 key ∈ ALLOWED_LABELS。

    写入侧最后一道防线，防止高基数字段意外进入 TSDB 标签空间（C-METRIC-MODEL-1/2）。

    Args:
        labels: 待校验的标签字典。

    Raises:
        ValueError: 存在不在白名单内的标签 key。
    """
    disallowed = set(labels.keys()) - _ALLOWED_LABELS_SET
    if disallowed:
        raise ValueError(
            f"Labels contain disallowed keys (high-cardinality fields not permitted in TSDB): {sorted(disallowed)}"
        )


__all__ = [
    "ALLOWED_LABELS",
    "RESOURCE_LABELS",
    "SEMANTIC_LABELS",
    "assert_label_cardinality_safe",
    "base_labels",
    "derive_resource_labels",
]
