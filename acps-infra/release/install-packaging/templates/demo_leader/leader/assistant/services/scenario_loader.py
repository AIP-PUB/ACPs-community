"""
Leader Agent Platform - Scenario Loader

本模块负责加载和管理场景配置，包括：
- base/prompts.toml：基础场景的提示词
- expert/<scenario>/domain.toml：专业场景的领域元数据
- expert/<scenario>/prompts.toml：专业场景的提示词覆盖
"""

import hashlib
import logging
from pathlib import Path
from typing import Any

from leader.runtime_paths import resolve_scenario_root

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        raise ImportError("tomllib is required for Python < 3.11. Install it with: pip install tomli")

from ..models import ScenarioBrief, ScenarioRuntime, now_iso

logger = logging.getLogger(__name__)


class ScenarioLoader:
 """
    场景配置加载器。

    负责：
    1. 加载 base 场景配置
    2. 发现并加载所有 expert 场景
    3. 提供配置合并能力（expert 覆盖 base）
    4. 生成 ScenarioBrief 列表供 LLM-1 使用
 """

    def __init__(self, scenario_root: Path | None = None):
 """
        初始化场景加载器。

        Args:
            scenario_root: 场景配置根目录。默认为 leader/scenario/
 """
        if scenario_root is None:
            self.scenario_root = resolve_scenario_root
        else:
            self.scenario_root = Path(scenario_root)

        self._base_scenario: ScenarioRuntime | None = None
        self._expert_scenarios: dict[str, ScenarioRuntime] = {}
        self._scenario_briefs: list[ScenarioBrief] = []

    @property
    def base_scenario(self) -> ScenarioRuntime:
 """获取基础场景（延迟加载）。"""
        if self._base_scenario is None:
            self._base_scenario = self._load_base_scenario
        return self._base_scenario

    @property
    def scenario_briefs(self) -> list[ScenarioBrief]:
 """获取所有专业场景的简要信息列表（用于 LLM-1）。"""
        if not self._scenario_briefs:
            self._discover_expert_scenarios
        return self._scenario_briefs

    def get_expert_scenario(self, scenario_id: str) -> ScenarioRuntime | None:
 """
        获取指定的专业场景配置。

        Args:
            scenario_id: 场景 ID

        Returns:
            ScenarioRuntime 或 None（如果场景不存在）
 """
        if scenario_id not in self._expert_scenarios:
            self._load_expert_scenario(scenario_id)
        return self._expert_scenarios.get(scenario_id)

    def get_merged_prompts(self, scenario_id: str | None = None) -> dict[str, str]:
 """
        获取合并后的提示词配置。

        合并规则：expert 的同名 key 覆盖 base。

        Args:
            scenario_id: 专业场景 ID（None 表示只使用 base）

        Returns:
            合并后的 prompts 字典
 """
        base_prompts = self.base_scenario.prompts.copy

        if scenario_id:
            expert = self.get_expert_scenario(scenario_id)
            if expert and expert.prompts:
                base_prompts.update(expert.prompts)

        return base_prompts

    def _load_base_scenario(self) -> ScenarioRuntime:
 """加载基础场景配置。"""
        base_path = self.scenario_root / "base"
        prompts_file = base_path / "prompts.toml"

        if not prompts_file.exists:
            logger.warning(f"Base prompts.toml not found at {prompts_file}")
            prompts = {}
            config_digest = None
        else:
            prompts, config_digest = self._load_prompts_file(prompts_file)

        return ScenarioRuntime(
            id="base",
            kind="base",
            version="1.0.0",
            loaded_at=now_iso,
            source_path=str(base_path),
            config_digest=config_digest,
            prompts=prompts,
            domain_meta=None,
            static_partners=None,
        )

    def _discover_expert_scenarios(self) -> None:
 """发现所有专业场景并构建 briefs 列表。"""
        expert_root = self.scenario_root / "expert"

        if not expert_root.exists:
            logger.warning(f"Expert scenarios directory not found: {expert_root}")
            return

        self._scenario_briefs = []

        for scenario_dir in expert_root.iterdir:
            if not scenario_dir.is_dir:
                continue

            domain_file = scenario_dir / "domain.toml"
            if not domain_file.exists:
                logger.debug(f"Skipping {scenario_dir.name}: no domain.toml")
                continue

            try:
                brief = self._load_scenario_brief(domain_file)
                if brief:
                    self._scenario_briefs.append(brief)
                    logger.info(f"Discovered expert scenario: {brief.id} - {brief.name}")
            except Exception as e:
                logger.error(f"Failed to load scenario brief from {domain_file}: {e}")

    def _load_scenario_brief(self, domain_file: Path) -> ScenarioBrief | None:
 """从 domain.toml 加载场景简要信息。"""
        try:
            with open(domain_file, "rb") as f:
                data = tomllib.load(f)

            meta = data.get("meta", {})
            routing = data.get("routing", {})

            if not meta.get("id"):
                logger.warning(f"Missing meta.id in {domain_file}")
                return None

            return ScenarioBrief(
                id=meta.get("id"),
                name=meta.get("name", meta.get("id")),
                description=meta.get("description", ""),
                keywords=routing.get("keywords", []),
            )
        except Exception as e:
            logger.error(f"Error parsing {domain_file}: {e}")
            return None

    def _load_expert_scenario(self, scenario_id: str) -> None:
 """加载指定的专业场景。"""
        scenario_path = self.scenario_root / "expert" / scenario_id

        if not scenario_path.exists:
            logger.warning(f"Expert scenario not found: {scenario_id}")
            return

 # 加载 domain.toml
        domain_file = scenario_path / "domain.toml"
        domain_meta = None
        if domain_file.exists:
            try:
                with open(domain_file, "rb") as f:
                    domain_meta = tomllib.load(f)
            except Exception as e:
                logger.error(f"Failed to load domain.toml for {scenario_id}: {e}")

 # 加载 prompts.toml
        prompts_file = scenario_path / "prompts.toml"
        if prompts_file.exists:
            prompts, config_digest = self._load_prompts_file(prompts_file)
        else:
            prompts = {}
            config_digest = None

 # 构建版本信息
        version = domain_meta.get("meta", {}).get("version", "1.0.0") if domain_meta else "1.0.0"

        self._expert_scenarios[scenario_id] = ScenarioRuntime(
            id=scenario_id,
            kind="expert",
            version=version,
            loaded_at=now_iso,
            source_path=str(scenario_path),
            config_digest=config_digest,
            prompts=prompts,
            domain_meta=domain_meta,
            static_partners=None,
        )

        logger.info(f"Loaded expert scenario: {scenario_id}")

    def _load_prompts_file(self, prompts_file: Path) -> tuple[dict[str, str], str]:
 """
        加载并展平 prompts.toml 文件。

        Returns:
            (prompts_dict, config_digest)
 """
        try:
            with open(prompts_file, "rb") as f:
                content = f.read

            data = tomllib.loads(content.decode("utf-8"))
            config_digest = hashlib.md5(content).hexdigest[:8]

 # 展平嵌套结构为 "section.key" 格式
            prompts = self._flatten_prompts(data)

            return prompts, config_digest
        except Exception as e:
            logger.error(f"Failed to load prompts from {prompts_file}: {e}")
            return {}, ""

    def _flatten_prompts(self, data: dict[str, Any], prefix: str = "") -> dict[str, str]:
 """
        展平嵌套的 prompts 配置。

        例如：
        [intent_analysis]
        system = "..."
        llm_profile = "llm.fast"

        变为：
        "intent_analysis.system" -> "..."
        "intent_analysis.llm_profile" -> "llm.fast"
 """
        result = {}

        for key, value in data.items:
            full_key = f"{prefix}.{key}" if prefix else key

            if isinstance(value, dict):
                result.update(self._flatten_prompts(value, full_key))
            elif isinstance(value, str):
                result[full_key] = value

        return result

    def get_prompt(
        self,
        call_point: str,
        key: str = "system",
        scenario_id: str | None = None,
    ) -> str | None:
 """
        获取指定 LLM 调用点的 prompt。

        Args:
            call_point: LLM 调用点名称（如 "intent_analysis"）
            key: prompt 键名（默认 "system"）
            scenario_id: 专业场景 ID（可选）

        Returns:
            prompt 字符串或 None
 """
        prompts = self.get_merged_prompts(scenario_id)
        full_key = f"{call_point}.{key}"
        return prompts.get(full_key)

    def get_llm_profile(
        self,
        call_point: str,
        scenario_id: str | None = None,
    ) -> str:
 """
        获取指定 LLM 调用点的 profile。

        Args:
            call_point: LLM 调用点名称
            scenario_id: 专业场景 ID（可选）

        Returns:
            LLM profile 名称，默认 "llm.default"
 """
        prompts = self.get_merged_prompts(scenario_id)
        full_key = f"{call_point}.llm_profile"
        return prompts.get(full_key, "llm.default")

    def get_persona_system(self, scenario_id: str | None = None) -> str:
 """
        获取系统人设 prompt。

        Args:
            scenario_id: 专业场景 ID（可选）

        Returns:
            persona.system 的内容
 """
        prompts = self.get_merged_prompts(scenario_id)
        return prompts.get("persona.system", "")


# =============================================================================
# 模块级工厂函数
# =============================================================================

_scenario_loader_instance: ScenarioLoader | None = None


def get_scenario_loader -> ScenarioLoader:
 """
    获取场景加载器单例实例。

    Returns:
        ScenarioLoader 实例
 """
    global _scenario_loader_instance
    if _scenario_loader_instance is None:
        _scenario_loader_instance = ScenarioLoader
    return _scenario_loader_instance
