from __future__ import annotations

from pathlib import Path

from assistant.services import scenario_loader as scenario_loader_mod
from assistant.services.scenario_loader import ScenarioLoader, get_scenario_loader


def _write_scenario_tree(root: Path) -> None:
    base = root / "base"
    expert = root / "expert"
    travel = expert / "travel"
    invalid = expert / "invalid"
    no_domain = expert / "no-domain"
    base.mkdir(parents=True)
    travel.mkdir(parents=True)
    invalid.mkdir(parents=True)
    no_domain.mkdir(parents=True)

    (base / "prompts.toml").write_text(
        """
[persona]
system = "base persona"

[planner]
system = "base planner"
llm_profile = "llm.fast"
ignored_number = 42
""",
        encoding="utf-8",
    )
    (travel / "domain.toml").write_text(
        """
[meta]
id = "travel"
name = "Travel"
description = "Trip planning"
version = "2.2.0"

[routing]
keywords = ["hotel", "flight"]
""",
        encoding="utf-8",
    )
    (travel / "prompts.toml").write_text(
        """
[planner]
system = "travel planner"

[completion_gate]
system = "travel gate"
""",
        encoding="utf-8",
    )
    (invalid / "domain.toml").write_text('[meta]\nname = "missing id"\n', encoding="utf-8")


def test_scenario_loader_discovers_and_merges_prompts(tmp_path: Path) -> None:
    _write_scenario_tree(tmp_path)
    loader = ScenarioLoader(tmp_path)

    briefs = loader.scenario_briefs
    assert [brief.id for brief in briefs] == ["travel"]
    assert briefs[0].keywords == ["hotel", "flight"]

    base = loader.base_scenario
    assert base.id == "base"
    assert base.kind == "base"
    assert base.config_digest
    assert loader.get_prompt("persona") == "base persona"
    assert loader.get_llm_profile("planner") == "llm.fast"

    expert = loader.get_expert_scenario("travel")
    assert expert is not None
    assert expert.version == "2.2.0"
    assert expert.domain_meta is not None
    assert expert.prompts["planner.system"] == "travel planner"

    merged = loader.get_merged_prompts("travel")
    assert merged["persona.system"] == "base persona"
    assert merged["planner.system"] == "travel planner"
    assert merged["completion_gate.system"] == "travel gate"
    assert loader.get_persona_system("travel") == "base persona"


def test_scenario_loader_handles_missing_and_malformed_files(tmp_path: Path) -> None:
    loader = ScenarioLoader(tmp_path)

    assert loader.base_scenario.prompts == {}
    assert loader.base_scenario.config_digest is None
    assert loader.scenario_briefs == []
    assert loader.get_expert_scenario("missing") is None

    expert_dir = tmp_path / "expert" / "broken"
    expert_dir.mkdir(parents=True)
    (expert_dir / "domain.toml").write_text("[meta\n", encoding="utf-8")
    (expert_dir / "prompts.toml").write_text("[persona\n", encoding="utf-8")

    assert loader._load_scenario_brief(expert_dir / "domain.toml") is None
    loader._load_expert_scenario("broken")
    broken = loader.get_expert_scenario("broken")
    assert broken is not None
    assert broken.domain_meta is None
    assert broken.prompts == {}
    assert broken.config_digest == ""


def test_get_scenario_loader_singleton(monkeypatch) -> None:
    monkeypatch.setattr(scenario_loader_mod, "_scenario_loader_instance", None)

    first = get_scenario_loader()
    second = get_scenario_loader()

    assert first is second
