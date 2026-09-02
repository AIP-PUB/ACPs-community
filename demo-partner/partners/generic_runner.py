import asyncio
import contextlib
import json
import os
import re
import time as _time
import tomllib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import structlog
from acps_sdk.aip.aip_base_model import (
    FileDataItem,
    Product,
    TaskCommand,
    TaskCommandType,
    TaskResult,
    TaskState,
    TaskStatus,
    TextDataItem,
)
from acps_sdk.aip.aip_rpc_model import JSONRPCError, RpcRequest, RpcResponse
from acps_sdk.aip.aip_rpc_server import CommandHandlers, DefaultHandlers
from acps_sdk.amp import (
    AccessEmitter,
    AuditAction,
    AuditActor,
    AuditBody,
    AuditEmitter,
    AuditResult,
    AuditTarget,
    HeartbeatEmitter,
    MessageEmitter,
    MetricsEmitter,
    SystemEmitter,
)
from acps_sdk.amp.metrics_demo import DemoMetricsSampler
from acps_sdk.amp.signer import AuditSigner, CertificateAuditSigner, load_signer_from_keys_json
from dotenv import load_dotenv
from openai import AsyncOpenAI

# --- Logging Setup ---
BEIJING_TZ = timezone(timedelta(hours=8))

# --- Audit: 终止状态集合与 resultStatus 映射 ---
_TERMINAL_STATES = {TaskState.Completed, TaskState.Rejected, TaskState.Failed, TaskState.Canceled}
_STATUS_MAP: dict[TaskState, Literal["success", "failure", "unknown"]] = {
    TaskState.Completed: "success",
    TaskState.Rejected: "failure",
    TaskState.Failed: "failure",
    TaskState.Canceled: "failure",
}
LLM_STAGE_TEMPERATURES = {
    "decision": 0.2,
    "analysis": 0.2,
    "production": 0.6,
    "skill": 0.6,
}
LLM_REQUIRED_FIELDS = ("api_key", "base_url", "model")
SKILLS_CONFIG_META_KEYS = {"slot_labels"}
CHINA_TRANSPORT_INTERCITY_SKILL = "china_transport.intercity-transportation-planning"
CHINA_TRANSPORT_ROUTE_SKILL = "china_transport.route-optimization"


def resolve_amp_log_dir() -> Path:
    """解析 AMP jsonl 日志目录。

    image-mode 由部署显式设置 AMP_LOG_DIR（如 /opt/acps/app/logs）；
    未设置时回退到仓库根目录下的 logs/（本地开发）。
    """
    amp_log_dir = os.environ.get("AMP_LOG_DIR")
    if amp_log_dir:
        return Path(amp_log_dir)

    return Path(__file__).parent.parent / "logs"


def truncate_text(text: str, max_len: int = 300) -> str:
    """截断长文本用于日志输出"""
    if not text:
        return "<empty>"
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"...[truncated, total {len(text)} chars]"


def extract_json_from_llm_response(response: str) -> str:
    """
    从 LLM 响应中提取 JSON 字符串。

    处理以下情况：
    1. 响应被包裹在 ```json ... ``` 代码块中
    2. JSON 前后有额外的文本（如解释性说明）
    3. 响应中包含注释

    Args:
        response: LLM 原始响应

    Returns:
        提取出的 JSON 字符串
    """
    response = response.strip()

    # 尝试从 markdown 代码块中提取
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", response)
    if json_match:
        response = json_match.group(1).strip()

    # 如果响应不是以 { 开头，尝试找到第一个 {
    if not response.startswith("{"):
        start_idx = response.find("{")
        if start_idx != -1:
            response = response[start_idx:]

    # 找到匹配的最后一个 }（处理 JSON 后有额外文本的情况）
    if response.startswith("{"):
        brace_count = 0
        end_idx = -1
        for i, char in enumerate(response):
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i
                    break
        if end_idx != -1:
            response = response[: end_idx + 1]

    # 移除 JSON 中的注释（LLM 有时会添加 // 或 /* */ 注释）
    response = re.sub(r"//.*?(?=\n|$)", "", response)  # 移除 // 注释
    response = re.sub(r"/\*.*?\*/", "", response, flags=re.DOTALL)  # 移除 /* */ 注释

    # 将中文引号替换为英文引号（LLM 有时会在字符串值中使用中文引号）
    response = response.replace(
        """, '\\"')  # 中文左双引号
    response = response.replace(""",
        '\\"',
    )  # 中文右双引号
    response = response.replace("'", "'")  # 中文左单引号
    response = response.replace("'", "'")  # 中文右单引号

    return response.strip()


# --- Data Structures ---


@dataclass
class TaskContext:
    task: TaskResult
    last_updated_at: datetime = field(default_factory=lambda: datetime.now(BEIJING_TZ))
    requirements: dict[str, Any] | None = None
    running_future: asyncio.Task[Any] | None = None


def _deep_merge_requirements(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """
    Deep merge requirements dictionaries.
    For nested dicts (like 'global'), merge instead of replace.
    For non-null values in update, they override base values.
    """
    result = base.copy()
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            # Deep merge nested dicts
            merged_dict = result[key].copy()
            for k, v in value.items():
                # Only update if new value is not None/null
                if v is not None:
                    merged_dict[k] = v
            result[key] = merged_dict
        else:
            result[key] = value
    return result


class GenericRunner:
    def __init__(self, agent_name: str, base_dir: str):
        self.agent_name = agent_name
        self.base_dir = base_dir
        self.tasks: dict[str, TaskContext] = {}

        self._load_project_env()

        # 状态变化回调（旧 API，向后兼容；内部由 _state_change_listeners 统一管理）
        self._on_state_change_callback: Callable[[TaskResult], Coroutine[Any, Any, None]] | None = None
        # 多监听者列表（stream / notification / group 可各自注册）
        self._state_change_listeners: list[Callable[[TaskResult], Coroutine[Any, Any, None]]] = []
        # 异步监听者任务集合（防止 GC 回收 fire-and-forget 任务）
        self._bg_listener_tasks: set[asyncio.Task[None]] = set()

        # Load configurations
        self.acs = self._load_acs()
        self.config = self._load_config()
        self.prompts = self._load_prompts()
        self.skills_config = self._load_skills_config()

        # Setup Logger
        self.logger = structlog.get_logger(agent_name)

        # Setup Audit Emitter（从 acs.json 读取 AIC，日志写入 logs/ 目录）
        self._aic: str = self.acs.get("aic", "")
        _amp_log_dir = resolve_amp_log_dir()
        _audit_log_file = _amp_log_dir / f"amp_audit_{agent_name}.jsonl"

        # 签名器初始化：优先使用 CA 证书（{base_dir}/client.pem + client.key），
        # 回退到共享 audit_keys.json（开发 mock 模式）。
        # 可通过环境变量 AMP_AUDIT_CERT_FILE / AMP_AUDIT_KEY_FILE 覆盖证书路径。
        _cert_file = Path(os.environ.get("AMP_AUDIT_CERT_FILE", str(Path(self.base_dir) / "client.pem")))
        _key_file = Path(os.environ.get("AMP_AUDIT_KEY_FILE", str(Path(self.base_dir) / "client.key")))
        _signer: AuditSigner | None = None
        if _cert_file.exists() and _key_file.exists():
            try:
                _signer = CertificateAuditSigner(
                    private_key_pem=_key_file.read_text(encoding="utf-8"),
                    cert_pem=_cert_file.read_text(encoding="utf-8"),
                )
                self.logger.info(
                    "AMP 审计签名器：CA 证书模式",
                    kid=_signer.kid,
                    alg=_signer.alg,
                )
            except Exception as exc:
                self.logger.warning("CA 证书签名器初始化失败，回退到 audit_keys.json 模式", error=str(exc))
                _signer = None
        if _signer is None:
            _audit_keys_file = Path(
                os.environ.get(
                    "AMP_AUDIT_KEYS_FILE",
                    str(Path(__file__).parent.parent.parent / "monitor-server" / "config" / "audit_keys.json"),
                )
            )
            _signer = load_signer_from_keys_json(_audit_keys_file, self._aic)
        self._audit_emitter = AuditEmitter(_audit_log_file, aic=self._aic, signer=_signer)

        # Setup Heartbeat Emitter（每 Agent 独立文件，避免并发写交错）
        _hb_file = _amp_log_dir / f"amp_heartbeat_{agent_name}.jsonl"
        self._heartbeat_emitter = HeartbeatEmitter(_hb_file, aic=self._aic)
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Setup Metrics Emitter（每 Agent 独立文件，per-agent 路径互不冲突）
        _metrics_file = _amp_log_dir / f"amp_metrics_{agent_name}.jsonl"
        _metrics_resource = {
            "service.name": f"demo-partner-{agent_name}",
            "service.namespace": "acps-demo",
            "deployment.environment.name": "dev",
        }
        self._metrics_sampler = DemoMetricsSampler(aic=self._aic)
        self._metrics_emitter = MetricsEmitter(
            _metrics_file,
            aic=self._aic,
            sampler=self._metrics_sampler,
            resource=_metrics_resource,
        )
        self._metrics_task: asyncio.Task[None] | None = None

        # Setup Access Emitter（每 Agent 独立文件，事件驱动无周期任务）
        _access_file = _amp_log_dir / f"amp_access_{agent_name}.jsonl"
        self._access_emitter = AccessEmitter(_access_file, aic=self._aic)
        self._service_name = f"demo-partner-{agent_name}"

        # Setup Message Emitter（每 Agent 独立文件，事件驱动无周期任务）
        _message_file = _amp_log_dir / f"amp_message_{agent_name}.jsonl"
        self._message_emitter = MessageEmitter(_message_file, aic=self._aic)

        # Setup System Emitter（每 Agent 独立文件，事件驱动无周期任务）
        _system_file = _amp_log_dir / f"amp_system_{agent_name}.jsonl"
        _system_resource = {
            "service.name": f"demo-partner-{agent_name}",
            "service.namespace": "acps-demo",
            "deployment.environment.name": "dev",
        }
        self._system_emitter = SystemEmitter(
            _system_file,
            aic=self._aic,
            resource=_system_resource,
        )

        # Setup LLM Clients
        self.llm_clients: dict[str, AsyncOpenAI] = {}
        self._setup_llm_clients()

        # Setup Command Handlers
        self.handlers = CommandHandlers(
            on_start=self.on_start,
            on_get=self.on_get,
            on_cancel=self.on_cancel,
            on_complete=self.on_complete,
            on_continue=self.on_continue,
        )

    def _load_acs(self) -> dict[str, Any]:
        path = Path(self.base_dir) / "acs.json"
        with path.open(encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]

    def _load_project_env(self) -> None:
        candidate_roots = [Path.cwd(), Path(self.base_dir).resolve()]
        visited: set[Path] = set()

        for root in candidate_roots:
            for current in (root, *root.parents):
                resolved_current = current.resolve()
                if resolved_current in visited:
                    continue
                visited.add(resolved_current)

                env_path = resolved_current / ".env"
                if env_path.exists():
                    load_dotenv(env_path, override=False)
                    return

    def _resolve_llm_value(
        self,
        profile_name: str,
        profile_data: dict[str, Any],
        field_name: str,
    ) -> str:
        env_key_name = f"{field_name}_env"
        env_var_name = profile_data.get(env_key_name)
        if isinstance(env_var_name, str) and env_var_name.strip():
            resolved_value = os.getenv(env_var_name.strip())
            if isinstance(resolved_value, str) and resolved_value.strip():
                return resolved_value
            raise ValueError(
                f"[{self.agent_name}] Missing environment variable {env_var_name} for llm.{profile_name}.{field_name}"
            )

        literal_value = profile_data.get(field_name)
        if isinstance(literal_value, str) and literal_value.strip():
            return literal_value

        raise ValueError(f"[{self.agent_name}] Missing llm.{profile_name}.{field_name} or {env_key_name}")

    def _resolve_llm_config(self, config: dict[str, Any]) -> None:
        llm_config = config.get("llm", {})
        if not isinstance(llm_config, dict):
            return

        for profile_name, profile_data in llm_config.items():
            if not isinstance(profile_data, dict):
                continue

            for field_name in LLM_REQUIRED_FIELDS:
                profile_data[field_name] = self._resolve_llm_value(
                    profile_name,
                    profile_data,
                    field_name,
                )

    def _load_config(self) -> dict[str, Any]:
        path = Path(self.base_dir) / "config.toml"
        with path.open("rb") as f:
            config = tomllib.load(f)

        self._resolve_llm_config(config)
        return config

    def _load_prompts(self) -> dict[str, Any]:
        path = Path(self.base_dir) / "prompts.toml"
        with path.open("rb") as f:
            return tomllib.load(f)

    def _load_skills_config(self) -> dict[str, Any]:
        path = Path(self.base_dir) / "skills.toml"
        if path.exists():
            with path.open("rb") as f:
                return tomllib.load(f)
        return {}

    def _setup_llm_clients(self) -> None:
        llm_config = self.config.get("llm", {})
        for profile_name, profile_data in llm_config.items():
            self.llm_clients[profile_name] = AsyncOpenAI(
                api_key=profile_data.get("api_key"),
                base_url=profile_data.get("base_url"),
                timeout=120.0,
            )

    def _get_llm_client(self, profile_name: str) -> AsyncOpenAI:
        if profile_name in self.llm_clients:
            return self.llm_clients[profile_name]
        if "default" in self.llm_clients:
            return self.llm_clients["default"]
        if self.llm_clients:
            return next(iter(self.llm_clients.values()))
        return AsyncOpenAI()

    def _get_model_name(self, profile_name: str) -> str:
        llm_config = self.config.get("llm", {})
        profile = llm_config.get(profile_name, {})
        return profile.get("model") or "gpt-3.5-turbo"

    def _get_llm_temperature(self, stage: str) -> float:
        return LLM_STAGE_TEMPERATURES.get(stage, LLM_STAGE_TEMPERATURES["production"])

    # --- Helper Methods ---

    def _configured_skill_ids(self) -> list[str]:
        return [skill_id for skill_id in self.skills_config if skill_id not in SKILLS_CONFIG_META_KEYS]

    def _canonicalize_skill_id(self, skill_id: str) -> str | None:
        normalized_skill_id = skill_id.strip()
        if not normalized_skill_id:
            return None

        configured_skill_ids = self._configured_skill_ids()
        if normalized_skill_id in configured_skill_ids:
            return normalized_skill_id

        suffix = normalized_skill_id.rsplit(".", 1)[-1]
        preferred_skill_id = f"{self.agent_name}.{suffix}"
        if preferred_skill_id in configured_skill_ids:
            return preferred_skill_id

        suffix_matches = [candidate for candidate in configured_skill_ids if candidate.endswith(f".{suffix}")]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        return None

    def _split_city_sequence(self, city_sequence: Any) -> list[str]:
        if not isinstance(city_sequence, str) or not city_sequence.strip():
            return []

        raw_tokens = re.split(
            r"\s*(?:->|→|—>|>|,|，|、|/|至|到)\s*",
            city_sequence.strip(),
        )
        normalized_tokens = []
        for raw_token in raw_tokens:
            token = raw_token.strip("()（）[]【】 ").strip()
            if token:
                normalized_tokens.append(token)
        return normalized_tokens

    def _infer_destination_from_sequence(self, from_city: Any, city_tokens: list[str]) -> str | None:
        if len(city_tokens) < 2:
            return None

        normalized_from_city = str(from_city).strip() if from_city else ""
        for city_token in city_tokens[1:]:
            if city_token != normalized_from_city:
                return city_token
        return city_tokens[1]

    def _normalize_china_transport_slots(self, global_slots: dict[str, Any]) -> dict[str, Any]:
        normalized_slots = dict(global_slots)
        city_tokens = self._split_city_sequence(normalized_slots.get("city_sequence"))
        if not city_tokens:
            return normalized_slots

        if not normalized_slots.get("from_city"):
            normalized_slots["from_city"] = city_tokens[0]

        inferred_destination = self._infer_destination_from_sequence(normalized_slots.get("from_city"), city_tokens)
        current_destination = normalized_slots.get("to_city")
        current_origin = normalized_slots.get("from_city")

        if inferred_destination and (
            not current_destination or str(current_destination).strip() == str(current_origin).strip()
        ):
            normalized_slots["to_city"] = inferred_destination

        return normalized_slots

    def _normalize_china_transport_skill(self, skill_id: str, global_slots: dict[str, Any]) -> str:
        if skill_id != CHINA_TRANSPORT_ROUTE_SKILL:
            return skill_id
        if global_slots.get("luggage_special"):
            return skill_id

        city_tokens = self._split_city_sequence(global_slots.get("city_sequence"))
        if not city_tokens:
            if global_slots.get("from_city") and global_slots.get("to_city"):
                return CHINA_TRANSPORT_INTERCITY_SKILL
            return skill_id

        distinct_city_count = len(set(city_tokens))
        is_round_trip = len(city_tokens) >= 3 and city_tokens[0] == city_tokens[-1]
        if distinct_city_count <= 2 and (
            is_round_trip or (global_slots.get("from_city") and global_slots.get("to_city"))
        ):
            return CHINA_TRANSPORT_INTERCITY_SKILL
        return skill_id

    def _normalize_selected_skills(self, selected_skills: Any, global_slots: dict[str, Any]) -> list[str]:
        if not isinstance(selected_skills, list):
            return []

        normalized_selected_skills = []
        for raw_skill_id in selected_skills:
            if not isinstance(raw_skill_id, str):
                continue
            canonical_skill_id = self._canonicalize_skill_id(raw_skill_id)
            if canonical_skill_id is None:
                continue
            if self.agent_name == "china_transport":
                canonical_skill_id = self._normalize_china_transport_skill(
                    canonical_skill_id,
                    global_slots,
                )
            if canonical_skill_id not in normalized_selected_skills:
                normalized_selected_skills.append(canonical_skill_id)
        return normalized_selected_skills

    def _normalize_requirements(self, requirements: dict[str, Any]) -> dict[str, Any]:
        normalized_requirements = dict(requirements)

        raw_global_slots = requirements.get("global", {})
        global_slots = dict(raw_global_slots) if isinstance(raw_global_slots, dict) else {}
        if self.agent_name == "china_transport":
            global_slots = self._normalize_china_transport_slots(global_slots)
        normalized_requirements["global"] = global_slots

        normalized_requirements["selectedSkills"] = self._normalize_selected_skills(
            requirements.get("selectedSkills", []),
            global_slots,
        )
        return normalized_requirements

    def add_state_change_listener(self, listener: Callable[[TaskResult], Coroutine[Any, Any, None]]) -> None:
        """注册状态变更监听者（去重；同一 listener 只会注册一次）。"""
        if listener not in self._state_change_listeners:
            self._state_change_listeners.append(listener)

    def remove_state_change_listener(self, listener: Callable[[TaskResult], Coroutine[Any, Any, None]]) -> None:
        """取消注册状态变更监听者（不存在时静默忽略）。"""
        with contextlib.suppress(ValueError):
            self._state_change_listeners.remove(listener)

    def set_state_change_callback(self, callback: Callable[[TaskResult], Coroutine[Any, Any, None]]) -> None:
        """
        设置状态变化回调函数（向后兼容 API）。

        等价于 add_state_change_listener；旧代码可直接调用，
        不会覆盖已经通过 add_state_change_listener 注册的其他监听者。

        回调函数签名: async def callback(task_result: TaskResult) -> None
        """
        self._on_state_change_callback = callback
        self.add_state_change_listener(callback)

    def start_heartbeat(self) -> None:
        """在已运行的事件循环中启动周期心跳任务（幂等）。"""
        interval = float(os.environ.get("AMP_HEARTBEAT_INTERVAL_SECONDS", "15"))
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_emitter.run_periodic(interval),
                name=f"amp-heartbeat-{self._aic}",
            )
            self.logger.info("AMP heartbeat started", aic=self._aic, interval=interval)

    async def stop_heartbeat(self) -> None:
        """取消周期心跳任务。"""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

    def start_metrics(self) -> None:
        """在已运行的事件循环中启动周期指标发射任务（幂等）。"""
        interval = float(os.environ.get("AMP_METRICS_INTERVAL_SECONDS", "30"))
        if self._metrics_task is None or self._metrics_task.done():
            self._metrics_task = asyncio.create_task(
                self._metrics_emitter.run_periodic(interval),
                name=f"amp-metrics-{self._aic}",
            )
            self.logger.info("AMP metrics started", aic=self._aic, interval=interval)

    async def stop_metrics(self) -> None:
        """取消周期指标发射任务。"""
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metrics_task
            self._metrics_task = None

    async def close_llm_clients(self) -> None:
        """关闭 AsyncOpenAI 客户端，避免测试与进程退出时遗留连接。"""
        for client in self.llm_clients.values():
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                self.logger.warning("LLM client close failed", exc_info=True)

    async def shutdown(self) -> None:
        """清理后台任务与网络客户端，供应用关闭与测试 teardown 复用。"""
        await self.stop_heartbeat()
        await self.stop_metrics()

        running_futures = [
            ctx.running_future
            for ctx in self.tasks.values()
            if ctx.running_future is not None and not ctx.running_future.done()
        ]
        for future in running_futures:
            future.cancel()
        for future in running_futures:
            with contextlib.suppress(asyncio.CancelledError):
                await future
        for ctx in self.tasks.values():
            if ctx.running_future is not None and ctx.running_future.done():
                ctx.running_future = None

        listener_tasks = list(self._bg_listener_tasks)
        for task in listener_tasks:
            task.cancel()
        for task in listener_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bg_listener_tasks.clear()

        self._state_change_listeners.clear()
        self._on_state_change_callback = None
        await self.close_llm_clients()

    def _sample_metrics(self) -> dict[str, int]:
        """从 self.tasks 统计真实 active / queued 任务计数。

        Returns:
            {"active_tasks": int, "queued_tasks": int}
        """
        active = sum(1 for ctx in self.tasks.values() if ctx.task.status.state == TaskState.Working)
        queued = sum(
            1
            for ctx in self.tasks.values()
            if ctx.task.status.state in (TaskState.Accepted, TaskState.AwaitingCompletion)
        )
        return {"active_tasks": active, "queued_tasks": queued}

    def _update_task_status(
        self, task_id: str, new_state: TaskState, data_items: list[Any] | None = None
    ) -> TaskResult:
        ctx = self.tasks.get(task_id)
        if not ctx:
            raise ValueError(f"Task {task_id} not found")

        new_status = TaskStatus(
            state=new_state,
            stateChangedAt=datetime.now(BEIJING_TZ).isoformat(),
            dataItems=data_items or [],
        )
        ctx.task.status = new_status
        if ctx.task.statusHistory:
            ctx.task.statusHistory.append(new_status)
        else:
            ctx.task.statusHistory = [new_status]

        ctx.last_updated_at = datetime.now(BEIJING_TZ)

        # B2: 审计——任务进入终止状态（同步方法，使用 emit_sync）
        if new_state in _TERMINAL_STATES:
            _reason_text: str | None = None
            for _item in data_items or []:
                _text = getattr(_item, "text", None)
                if _text:
                    _reason_text = _text
                    break
            try:
                self._audit_emitter.emit_sync(
                    AuditBody(
                        actor=AuditActor(id=self._aic or "system", type="agent"),
                        action=AuditAction(name="task_state_transition", type="aip_protocol"),
                        target=AuditTarget(type="task", id=task_id),
                        result=AuditResult(
                            status=_STATUS_MAP[new_state],
                            reason=_reason_text,
                        ),
                    ),
                    trace_id=ctx.task.sessionId,
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("Audit emit B2 failed", exc_info=True)

        # 触发所有已注册的状态变更监听者（fire-and-forget，异常互相隔离）
        # _on_state_change_callback 已经通过 set_state_change_callback → add_state_change_listener
        # 加入 _state_change_listeners，所以只需遍历 _state_change_listeners 即可。
        listeners = list(self._state_change_listeners)
        if listeners:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

            for listener in listeners:
                try:
                    if loop is not None and loop.is_running():
                        _t = asyncio.create_task(listener(ctx.task))
                        self._bg_listener_tasks.add(_t)
                        _t.add_done_callback(self._bg_listener_tasks.discard)
                    elif loop is not None:
                        loop.run_until_complete(listener(ctx.task))
                except Exception as e:
                    self.logger.warning(
                        "State change listener failed",
                        task_id=task_id,
                        listener=repr(listener),
                        error=str(e),
                    )

        return ctx.task

    def _add_command(self, task_id: str, command: TaskCommand) -> None:
        ctx = self.tasks.get(task_id)
        if ctx:
            if ctx.task.commandHistory:
                ctx.task.commandHistory.append(command)
            else:
                ctx.task.commandHistory = [command]
            ctx.last_updated_at = datetime.now(BEIJING_TZ)

    async def _call_llm(
        self,
        stage: str,
        profile_name: str,
        system_prompt: str,
        user_content: str | list[dict[str, Any]],
        *,
        task_id: str | None = None,
    ) -> str:
        client = self._get_llm_client(profile_name)
        model = self._get_model_name(profile_name)

        messages: list[Any] = [{"role": "system", "content": system_prompt}]
        if isinstance(user_content, str):
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_content})

        _t0 = _time.monotonic()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=self._get_llm_temperature(stage),
                # Doubao/seed models default to long thinking; disable for Partner stages.
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = response.choices[0].message.content or ""
            _elapsed = int((_time.monotonic() - _t0) * 1000)
            _token_total = getattr(getattr(response, "usage", None), "total_tokens", None)
            _body: dict[str, Any] = {
                "message": f"LLM call completed: stage={stage}, model={model}, elapsed={_elapsed}ms",
                "category": "llm",
                "component": "llm_client",
                "module": stage,
                "model": model,
                "tags": {"model": model, "stage": stage},
                "elapsed_ms": _elapsed,
            }
            if task_id:
                _body["tags"]["task_id"] = task_id
                _body["task_id"] = task_id
            if _token_total is not None:
                _body["token_total"] = _token_total
                _body["tags"]["token_total"] = str(_token_total)
            try:
                self._system_emitter.emit_sync(
                    _body,
                    severity_number=9,
                    severity_text="INFO",
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("System emit S-P1 failed", exc_info=True)
            return content
        except Exception as e:
            _elapsed = int((_time.monotonic() - _t0) * 1000)
            _error_type = type(e).__name__
            _error_msg = str(e)[:500]
            _err_body: dict[str, Any] = {
                "message": f"LLM call failed: stage={stage}, model={model}, error={_error_type}",
                "category": "llm",
                "component": "llm_client",
                "module": stage,
                "model": model,
                "tags": {"model": model, "stage": stage, "error_type": _error_type},
                "elapsed_ms": _elapsed,
                "error_type": _error_type,
                "error_message": _error_msg,
            }
            if task_id:
                _err_body["tags"]["task_id"] = task_id
                _err_body["task_id"] = task_id
            try:
                self._system_emitter.emit_sync(
                    _err_body,
                    severity_number=17,
                    severity_text="ERROR",
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("System emit S-P2 failed", exc_info=True)
            self.logger.exception("LLM call failed")
            raise

    def _extract_content_for_llm(
        self, command: TaskCommand, include_images: bool = False
    ) -> str | list[dict[str, Any]]:
        """
        Extracts content from command.
        If include_images is False, returns a single string (text only).
        If include_images is True, returns a list of content parts (text and images) for OpenAI Vision API.
        """
        texts = []
        images = []

        if command.dataItems:
            for item in command.dataItems:
                if isinstance(item, TextDataItem) and item.text:
                    texts.append(item.text)
                elif isinstance(item, dict) and item.get("text"):
                    texts.append(item["text"])
                elif isinstance(item, FileDataItem) and item.mimeType and item.mimeType.startswith("image/"):
                    if item.bytes:
                        images.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{item.mimeType};base64,{item.bytes}"},
                            }
                        )
                    elif item.uri:
                        images.append({"type": "image_url", "image_url": {"url": item.uri}})

        text_content = "\n".join(texts)

        if not include_images or not images:
            return text_content

        content_parts: list[dict[str, Any]] = [{"type": "text", "text": text_content}]
        content_parts.extend(images)
        return content_parts

    def _extract_text(self, command: TaskCommand) -> str:
        result = self._extract_content_for_llm(command, include_images=False)
        return result if isinstance(result, str) else ""

    # --- Command Handlers ---

    async def on_start(self, command: TaskCommand, task: TaskResult | None) -> TaskResult:
        task_id = command.taskId or command.id
        input_text = self._extract_text(command)
        self.logger.info("Start command received", task_id=task_id, input_preview=input_text[:100])

        if task:
            self.logger.debug("Task already exists, adding command to history", task_id=task_id)
            self._add_command(task_id, command)
            return task

        active_tasks = sum(
            1
            for ctx in self.tasks.values()
            if ctx.task.status.state
            in [
                TaskState.Working,
                TaskState.AwaitingInput,
                TaskState.AwaitingCompletion,
            ]
        )
        max_concurrent = self.config.get("concurrency", {}).get("max_concurrent_tasks", 10)

        if active_tasks >= max_concurrent:
            self.logger.warning(
                "Rejected: system busy",
                task_id=task_id,
                active_tasks=active_tasks,
                max=max_concurrent,
            )
            try:
                self._system_emitter.emit_sync(
                    {
                        "message": (f"Task rejected: system busy, active_tasks={active_tasks}, max={max_concurrent}"),
                        "category": "capacity",
                        "component": "runner",
                        "module": "start",
                        "tags": {"task_id": task_id},
                        "active_tasks": active_tasks,
                        "max_concurrent": max_concurrent,
                        "task_id": task_id,
                    },
                    severity_number=13,
                    severity_text="WARN",
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("System emit S-P5 failed", exc_info=True)
            new_task = TaskResult(
                id=f"result-{task_id}",
                sentAt=datetime.now(BEIJING_TZ).isoformat(),
                senderRole="partner",
                senderId=self._aic,
                taskId=task_id,
                sessionId=command.sessionId,
                status=TaskStatus(
                    state=TaskState.Rejected,
                    stateChangedAt=datetime.now(BEIJING_TZ).isoformat(),
                    dataItems=[TextDataItem(text="System busy")],
                ),
                commandHistory=[command],
                statusHistory=[],
            )
            self.tasks[task_id] = TaskContext(task=new_task)
            return new_task

        self.logger.debug("Creating new task, entering Accepted state", task_id=task_id)
        new_task = TaskResult(
            id=f"result-{task_id}",
            sentAt=datetime.now(BEIJING_TZ).isoformat(),
            senderRole="partner",
            senderId=self._aic,
            taskId=task_id,
            sessionId=command.sessionId,
            status=TaskStatus(
                state=TaskState.Accepted,
                stateChangedAt=datetime.now(BEIJING_TZ).isoformat(),
            ),
            commandHistory=[command],
            statusHistory=[
                TaskStatus(
                    state=TaskState.Accepted,
                    stateChangedAt=datetime.now(BEIJING_TZ).isoformat(),
                )
            ],
        )
        self.tasks[task_id] = TaskContext(task=new_task)

        # Run decision stage in background
        self.logger.debug("Starting decision stage in background", task_id=task_id)
        future = asyncio.create_task(self._run_decision_stage(task_id, command))
        self.tasks[task_id].running_future = future

        return self.tasks[task_id].task

    def _validate_skill_slots(self, skill_id: str, slots: dict[str, Any]) -> list[str]:
        """
        Validates if required slots are present for a skill based on skills.toml configuration.
        Returns a list of missing field names (using Chinese labels if available).
        """
        if not self.skills_config:
            return []

        skill_meta = self.skills_config.get(skill_id, {})
        if not skill_meta:
            # Try finding by key if the toml structure is flat or nested differently
            # Assuming structure: [skill_id] ...
            return []

        # Get slot labels for friendly names
        slot_labels = self.skills_config.get("slot_labels", {})

        def get_label(field_name: str) -> str:
            """Get Chinese label for a field, or return the original name."""
            return str(slot_labels.get(field_name, field_name))

        missing: list[str] = []

        # Check required_slots (all-of)
        for k in skill_meta.get("required_slots", []):
            v = slots.get(k)
            if not v:
                missing.append(get_label(k))

        # Check required_slots_anyof (one-of-groups)
        any_of = skill_meta.get("required_slots_anyof", [])
        if any_of:
            group_missing_list = []
            for group in any_of:
                group_missing = [k for k in group if not slots.get(k)]
                group_missing_list.append(group_missing)

            # If all groups have missing fields, return the one with fewest missing fields
            if not any(len(gm) == 0 for gm in group_missing_list):
                best_missing = min(group_missing_list, key=lambda gm: len(gm))
                missing.extend([get_label(k) for k in best_missing])

        return missing

    def _build_responsibilities_prompt(self) -> str:
        """
        Generates a responsibilities description string from ACS.
        """
        desc = self.acs.get("description", "")
        skills = self.acs.get("skills", [])

        skills_text = ""
        if skills:
            skills_list = []
            for skill in skills:
                name = skill.get("name", "")
                s_desc = skill.get("description", "")
                tags = ", ".join(skill.get("tags", []))
                skills_list.append(f"- {name}: {s_desc} (Tags: {tags})")
            skills_text = "\nSkills:\n" + "\n".join(skills_list)

        return f"{desc}\n{skills_text}".strip()

    async def _run_decision_stage(self, task_id: str, command: TaskCommand) -> None:
        """
        Decision 阶段：判断请求是否在服务范围内。

        - accept: 请求在服务范围内，进入 analysis 阶段
        - reject: 请求不在服务范围内（能力不匹配），直接进入 Rejected 终态

        注意：缺少必要信息不应该在此阶段 reject，而应该在 analysis 阶段进入 AwaitingInput。
        """
        self.tasks[task_id]
        decision_config = self.prompts.get("decision", {})

        input_modes = decision_config.get("input_modes", ["text"])
        include_images = "image" in input_modes

        user_content = self._extract_content_for_llm(command, include_images=include_images)

        output_schema = decision_config.get("output_schema", "")
        responsibilities = self._build_responsibilities_prompt()

        system_prompt = decision_config.get("system", "")
        system_prompt = system_prompt.replace("{{output_schema}}", output_schema)
        system_prompt = system_prompt.replace("{{responsibilities}}", responsibilities)

        # 调试日志：记录输入
        user_input_preview = user_content[:200] if isinstance(user_content, str) else str(user_content)[:200]
        self.logger.debug("Decision stage input", task_id=task_id, input_preview=user_input_preview)

        # If user_content is string, we can use template replacement if needed
        if isinstance(user_content, str):
            user_prompt_tmpl = decision_config.get("user", "")
            if "{{input}}" in user_prompt_tmpl:
                user_content = user_prompt_tmpl.replace("{{input}}", user_content)
            elif user_prompt_tmpl:
                user_content = f"{user_prompt_tmpl}\n\nUser Input:\n{user_content}"

        try:
            llm_response = await self._call_llm(
                "decision",
                decision_config.get("llm_profile", "default"),
                system_prompt,
                user_content,
                task_id=task_id,
            )

            # 调试日志：记录 LLM 原始响应
            self.logger.debug(
                "Decision LLM raw response",
                task_id=task_id,
                response_preview=llm_response[:500],
            )

            cleaned_response = extract_json_from_llm_response(llm_response)

            result = json.loads(cleaned_response)
            decision = result.get("decision")
            reason = result.get("reason", "")

            # Acceptance / flaky-LLM knob: force accept when Partner decision rejects
            # in-scope travel tasks (set PARTNER_FORCE_ACCEPT_DECISION=1 in env).
            if decision == "reject" and (os.environ.get("PARTNER_FORCE_ACCEPT_DECISION") or "").strip() in {
                "1",
                "true",
                "TRUE",
                "yes",
            }:
                self.logger.warning(
                    "PARTNER_FORCE_ACCEPT_DECISION: overriding reject → accept",
                    task_id=task_id,
                    original_reason=reason,
                )
                decision = "accept"
                reason = reason or "forced accept (PARTNER_FORCE_ACCEPT_DECISION)"

            # 调试日志：输出 decision 阶段的 LLM 决策
            self.logger.info(
                "Decision stage result",
                task_id=task_id,
                decision=decision,
                reason=reason,
            )

            # B1: 审计——收到 Start 命令，decision 完成
            _accepted = decision != "reject"
            try:
                await self._audit_emitter.emit(
                    AuditBody(
                        actor=AuditActor(id=command.senderId or "unknown", type="agent"),
                        action=AuditAction(name="receive_task_start", type="aip_protocol"),
                        target=AuditTarget(type="task", id=task_id),
                        result=AuditResult(
                            status="success" if _accepted else "failure",
                            reason=reason if not _accepted else None,
                        ),
                    ),
                    trace_id=command.sessionId,
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("Audit emit B1 failed", exc_info=True)

            if decision == "reject":
                # 请求不在服务范围内，进入 Rejected 终态
                self.logger.info("Request OUT OF SCOPE, entering Rejected state", task_id=task_id)
                self._update_task_status(task_id, TaskState.Rejected, [TextDataItem(text=reason)])
            else:
                # 请求在服务范围内，进入 analysis 阶段检查必要数据
                self.logger.debug("Request IN SCOPE, proceeding to analysis stage", task_id=task_id)
                self._update_task_status(task_id, TaskState.Working)
                await self._run_analysis_stage(task_id, command)  # Pass command to extract content again if needed

        except Exception as e:
            self.logger.exception("Decision stage failed", task_id=task_id)
            self._update_task_status(
                task_id,
                TaskState.Failed,
                [TextDataItem(text=f"Internal error: {e!s}")],
            )

    async def _run_analysis_stage(self, task_id: str, command: TaskCommand | str) -> None:
        """
        Analysis 阶段：提取需求槽位，判断是否缺少必要数据。

        此阶段假设请求已经通过 decision 阶段（在服务范围内）。
        - 如果缺少必要数据 → AwaitingInput（等待用户补充信息）
        - 如果数据完整 → 进入 production 阶段生成结果

        注意：analysis 阶段的 decision=reject 应该视为缺少必要信息，
        因为如果真的不在服务范围内，应该在 decision 阶段就被拒绝了。
        """
        ctx = self.tasks[task_id]
        analysis_config = self.prompts.get("analysis", {})

        input_modes = analysis_config.get("input_modes", ["text"])
        include_images = "image" in input_modes

        if isinstance(command, TaskCommand):
            user_content = self._extract_content_for_llm(command, include_images=include_images)
        else:
            user_content = command  # It's a string (text input)

        # 调试日志：记录输入
        user_input_preview = user_content[:200] if isinstance(user_content, str) else str(user_content)[:200]
        self.logger.debug("Analysis stage input", task_id=task_id, input_preview=user_input_preview)

        output_schema = analysis_config.get("output_schema", "")
        responsibilities = self._build_responsibilities_prompt()

        system_prompt = analysis_config.get("system", "")
        system_prompt = system_prompt.replace("{{output_schema}}", output_schema)
        system_prompt = system_prompt.replace("{{responsibilities}}", responsibilities)

        current_reqs = json.dumps(ctx.requirements, ensure_ascii=False) if ctx.requirements else "None"
        self.logger.debug("Analysis stage current_reqs", task_id=task_id, current_reqs=current_reqs)

        if isinstance(user_content, str):
            user_prompt_tmpl = analysis_config.get("user", "")
            if "{{input}}" in user_prompt_tmpl:
                user_content = user_prompt_tmpl.replace("{{input}}", user_content)
            elif user_prompt_tmpl:
                user_content = f"{user_prompt_tmpl}\n\nUser Input:\n{user_content}"

            user_content += f"\n\nCurrent Requirements: {current_reqs}"
        else:
            # It's a list of content parts. We need to append requirements text.
            user_content.append({"type": "text", "text": f"\n\nCurrent Requirements: {current_reqs}"})

        try:
            llm_response = await self._call_llm(
                "analysis",
                analysis_config.get("llm_profile", "default"),
                system_prompt,
                user_content,
                task_id=task_id,
            )

            # 调试日志：记录 LLM 原始响应
            self.logger.debug(
                "Analysis LLM raw response",
                task_id=task_id,
                response_preview=llm_response[:500],
            )

            cleaned_response = extract_json_from_llm_response(llm_response)

            result = json.loads(cleaned_response)

            decision = result.get("decision")
            reason = result.get("reason", "")
            requirements = result.get("requirements", {})

            # Same acceptance knob as decision stage: analysis reject can flake on
            # round-trip / scope wording even when the task is in-scope.
            if decision == "reject" and (os.environ.get("PARTNER_FORCE_ACCEPT_DECISION") or "").strip() in {
                "1",
                "true",
                "TRUE",
                "yes",
            }:
                self.logger.warning(
                    "PARTNER_FORCE_ACCEPT_DECISION: overriding analysis reject → accept",
                    task_id=task_id,
                    original_reason=reason,
                )
                decision = "accept"
                reason = reason or "forced accept (PARTNER_FORCE_ACCEPT_DECISION)"

            # 调试日志：记录解析结果
            self.logger.info(
                "Analysis stage result",
                task_id=task_id,
                decision=decision,
                reason=reason,
            )
            self.logger.debug(
                "Analysis LLM returned requirements",
                task_id=task_id,
                global_slots=requirements.get("global", {}),
            )

            # Use deep merge to preserve existing fields (especially in 'global')
            if ctx.requirements:
                self.logger.debug(
                    "Before merge - ctx.requirements",
                    task_id=task_id,
                    global_slots=ctx.requirements.get("global", {}),
                )
                ctx.requirements = _deep_merge_requirements(ctx.requirements, requirements)
                self.logger.debug(
                    "After merge - ctx.requirements",
                    task_id=task_id,
                    global_slots=ctx.requirements.get("global", {}),
                )
            else:
                ctx.requirements = requirements
                self.logger.debug(
                    "Initial requirements",
                    task_id=task_id,
                    global_slots=ctx.requirements.get("global", {}),
                )

            normalized_requirements = self._normalize_requirements(ctx.requirements)
            if normalized_requirements != ctx.requirements:
                self.logger.debug(
                    "Normalized requirements",
                    task_id=task_id,
                    selected_skills=normalized_requirements.get("selectedSkills", []),
                    global_slots=normalized_requirements.get("global", {}),
                )
                ctx.requirements = normalized_requirements

            # Validate skills if skills config exists
            selected_skills = ctx.requirements.get("selectedSkills", [])
            global_slots = ctx.requirements.get("global", {})

            # 如果 selectedSkills 为空但有 skills_config，使用第一个 skill 进行验证
            # 这确保即使 LLM 返回空的 selectedSkills，我们仍能检测缺失字段
            skills_to_validate = selected_skills
            if not skills_to_validate and self.skills_config:
                # 获取第一个非 slot_labels 的 skill
                skills_to_validate = [k for k in self.skills_config if k != "slot_labels"][:1]

            validation_missing = []
            if self.skills_config:
                for skill_id in skills_to_validate:
                    skill_missing = self._validate_skill_slots(skill_id, global_slots)
                    if skill_missing:
                        skill_name = self.skills_config.get(skill_id, {}).get("name", skill_id)
                        validation_missing.append(f"[{skill_name}] 缺少必填信息: {', '.join(skill_missing)}")

            missing_fields = requirements.get("missingFields", [])

            # Combine LLM detected missing fields with validation missing fields
            all_missing_reasons = []
            if missing_fields:
                all_missing_reasons.append(f"缺少必填信息: {', '.join(missing_fields)}")
            if validation_missing:
                all_missing_reasons.extend(validation_missing)

            # 调试日志：记录缺失字段
            self.logger.debug(
                "Missing fields from LLM",
                task_id=task_id,
                missing_fields=missing_fields,
            )
            self.logger.debug(
                "Missing fields from validation",
                task_id=task_id,
                validation_missing=validation_missing,
            )

            if decision == "reject" or all_missing_reasons:
                # Analysis 阶段的 reject 或有缺失字段时，进入 AwaitingInput 状态
                # 优先使用验证检测到的缺失字段，因为更准确和完整
                if all_missing_reasons:
                    reason_text = "缺少必填信息：" + "、".join(
                        [r.replace("缺少必填信息: ", "").replace("[", "").replace("]", "") for r in all_missing_reasons]
                    )
                else:
                    # 如果验证没有检测到缺失字段，使用 LLM 的 reason
                    reason_text = reason or "缺少必填信息，请提供更多详情"

                self.logger.info(
                    "Missing required fields, entering AwaitingInput",
                    task_id=task_id,
                    reason=reason_text,
                )
                self._update_task_status(
                    task_id,
                    TaskState.AwaitingInput,
                    [TextDataItem(text=reason_text)],
                )
            else:
                # 数据完整，进入 production 阶段
                self.logger.info(
                    "All required fields present, proceeding to production stage",
                    task_id=task_id,
                )
                await self._run_production_stage(task_id)

        except Exception as e:
            self.logger.exception("Analysis stage failed", task_id=task_id)
            self._update_task_status(
                task_id,
                TaskState.Failed,
                [TextDataItem(text=f"Internal error: {e!s}")],
            )

    async def _execute_skill(
        self,
        skill_id: str,
        slots_text: str,
        user_request: str,
        prod_config: dict[str, Any],
        global_slots: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> str:
        skill_name = self.skills_config.get(skill_id, {}).get("name", skill_id)
        _module = skill_id.rsplit(".", 1)[-1] if "." in skill_id else skill_id
        _t0 = _time.monotonic()

        skills_prompts = self.prompts.get("skills", {})
        skill_prompt_tmpl = skills_prompts.get(skill_id, {}).get("system", "")
        if not skill_prompt_tmpl:
            return ""

        # Inject variables - 支持两种格式的占位符
        skill_system_prompt = skill_prompt_tmpl.replace("{{slots_text}}", slots_text)
        skill_system_prompt = skill_system_prompt.replace("{{user_request}}", user_request)
        # 替换 {{input}} 占位符（用户原始请求）
        skill_system_prompt = skill_system_prompt.replace("{{input}}", user_request)

        # 替换单独的字段占位符 {{from_city}}, {{to_city}} 等
        for field_name, field_value in global_slots.items():
            placeholder = "{{" + field_name + "}}"
            # 如果值为 None 或空，用 "(未提供)" 替换
            display_value = str(field_value) if field_value else "(未提供)"
            skill_system_prompt = skill_system_prompt.replace(placeholder, display_value)

        self.logger.debug(
            "Skill prompt after variable injection",
            skill_id=skill_id,
            prompt_preview=skill_system_prompt[:500],
        )

        try:
            skill_response = await self._call_llm(
                "skill",
                prod_config.get("llm_profile", "default"),
                skill_system_prompt,
                "Please execute the skill based on system instructions.",
                task_id=task_id,
            )
            _elapsed = int((_time.monotonic() - _t0) * 1000)
            try:
                _s3_body: dict[str, Any] = {
                    "message": f"Skill executed: skill_id={skill_id}, elapsed={_elapsed}ms",
                    "category": "skill",
                    "component": "runner",
                    "module": _module,
                    "tags": {"skill_id": skill_id},
                    "elapsed_ms": _elapsed,
                    "skill_id": skill_id,
                }
                if task_id:
                    _s3_body["task_id"] = task_id
                    _s3_body["tags"]["task_id"] = task_id
                self._system_emitter.emit_sync(
                    _s3_body,
                    severity_number=9,
                    severity_text="INFO",
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("System emit S-P3 failed", exc_info=True)
            return f"【{skill_name}】\n{skill_response}"
        except Exception as e:
            _elapsed = int((_time.monotonic() - _t0) * 1000)
            try:
                _s4_body: dict[str, Any] = {
                    "message": f"Skill failed: skill_id={skill_id}, error={type(e).__name__}",
                    "category": "skill",
                    "component": "runner",
                    "module": _module,
                    "tags": {"skill_id": skill_id, "error_type": type(e).__name__},
                    "elapsed_ms": _elapsed,
                    "skill_id": skill_id,
                    "error_type": type(e).__name__,
                    "error_message": str(e)[:500],
                }
                if task_id:
                    _s4_body["task_id"] = task_id
                    _s4_body["tags"]["task_id"] = task_id
                self._system_emitter.emit_sync(
                    _s4_body,
                    severity_number=17,
                    severity_text="ERROR",
                    correlation_id=task_id,
                )
            except Exception:
                self.logger.warning("System emit S-P4 failed", exc_info=True)
            self.logger.exception("Skill execution failed", skill_id=skill_id)
            return f"【{skill_name}】\n(Execution Failed: {e!s})"

    async def _run_production_stage(self, task_id: str) -> None:
        ctx = self.tasks[task_id]
        prod_config = self.prompts.get("production", {})
        execution_mode = prod_config.get("execution_mode", "single_shot")

        try:
            final_output = ""

            if execution_mode in ["sequential_skills", "concurrent_skills"] and self.skills_config:
                # Multi-skill execution mode
                requirements = self._normalize_requirements(ctx.requirements or {})
                ctx.requirements = requirements
                selected_skills = requirements.get("selectedSkills", [])
                global_slots = requirements.get("global", {})

                # Prepare slots text for prompt injection
                slots_lines = [f"- {k}: {v}" for k, v in global_slots.items() if v]
                slots_text = "\n".join(slots_lines)

                # 详细日志：输出 production stage 的输入数据
                self.logger.info(
                    "Production stage starting",
                    task_id=task_id,
                    execution_mode=execution_mode,
                    selected_skills=selected_skills,
                    global_slots=global_slots,
                    slots_text=slots_text,
                )

                # Get original user request from command history
                user_request = ""
                if ctx.task.commandHistory:
                    # Simple concatenation of all user text commands
                    texts = []
                    for cmd in ctx.task.commandHistory:
                        texts.append(self._extract_text(cmd))
                    user_request = "\n---\n".join(texts)

                skill_outputs = []

                if execution_mode == "sequential_skills":
                    for skill_id in selected_skills:
                        out = await self._execute_skill(
                            skill_id,
                            slots_text,
                            user_request,
                            prod_config,
                            global_slots,
                            task_id=task_id,
                        )
                        if out:
                            skill_outputs.append(out)
                else:
                    # Concurrent execution
                    timeout = prod_config.get("concurrent_timeout", 120)
                    tasks = [
                        self._execute_skill(
                            skill_id,
                            slots_text,
                            user_request,
                            prod_config,
                            global_slots,
                            task_id=task_id,
                        )
                        for skill_id in selected_skills
                    ]
                    if tasks:
                        try:
                            results = await asyncio.wait_for(
                                asyncio.gather(*tasks, return_exceptions=True),
                                timeout=timeout,
                            )
                            for i, res in enumerate(results):
                                if isinstance(res, Exception):
                                    skill_id = selected_skills[i]
                                    skill_name = self.skills_config.get(skill_id, {}).get("name", skill_id)
                                    self.logger.error(
                                        "Skill failed in concurrent mode",
                                        skill_id=skill_id,
                                        error=str(res),
                                    )
                                    skill_outputs.append(f"【{skill_name}】\n(Execution Failed: {res!s})")
                                elif isinstance(res, str) and res:
                                    skill_outputs.append(res)
                        except TimeoutError:
                            self.logger.error("Concurrent skills execution timed out", timeout=timeout)
                            final_output = "(System Error: Skills execution timed out)"

                if not skill_outputs and not final_output:
                    final_output = "No skills executed or no output generated."
                elif skill_outputs:
                    final_output = "\n\n——\n".join(skill_outputs) + final_output
                    final_output += "\n\n【免责声明】价格/库存为演示估算，实际以官方/OTA 实时为准。"

            else:
                # Default single-shot mode
                requirements_json = json.dumps(ctx.requirements, ensure_ascii=False, indent=2)
                responsibilities = self._build_responsibilities_prompt()

                system_prompt = prod_config.get("system", "")
                system_prompt = system_prompt.replace("{{responsibilities}}", responsibilities)

                user_prompt = prod_config.get("user", "").replace("{{requirements}}", requirements_json)
                if "{{requirements}}" not in user_prompt:
                    user_prompt += f"\n\nRequirements:\n{requirements_json}"

                final_output = await self._call_llm(
                    "production",
                    prod_config.get("llm_profile", "default"),
                    system_prompt,
                    user_prompt,
                    task_id=task_id,
                )

            product = Product(
                id=f"prod-{datetime.now(BEIJING_TZ).timestamp()}",
                dataItems=[TextDataItem(text=final_output)],
            )
            ctx.task.products = [product]

            self.logger.info(
                "Production stage completed",
                task_id=task_id,
                product_id=product.id,
                output_preview=final_output[:200],
            )

            self._update_task_status(
                task_id,
                TaskState.AwaitingCompletion,
                [TextDataItem(text="Task completed. Please review.")],
            )

            self.logger.info(
                "State changed to AwaitingCompletion",
                task_id=task_id,
                products_count=len(ctx.task.products),
            )

        except Exception as e:
            self.logger.exception("Production stage failed", task_id=task_id)
            self._update_task_status(
                task_id,
                TaskState.Failed,
                [TextDataItem(text=f"Internal error: {e!s}")],
            )

    async def on_get(self, command: TaskCommand, task: TaskResult) -> TaskResult:
        # 详细日志：输出 Get 请求时的完整 Task 状态
        products_info = "None"
        products_preview = ""
        if task.products:
            products_info = f"{len(task.products)} product(s)"
            for i, prod in enumerate(task.products):
                if prod.dataItems:
                    for j, di in enumerate(prod.dataItems):
                        text_content = getattr(di, "text", str(di))
                        products_preview += f"\n  [prod{i}.item{j}]: {truncate_text(text_content, 200)}"

        data_items_info = "None"
        if task.status.dataItems:
            data_items_info = f"{len(task.status.dataItems)} item(s)"
            for i, di in enumerate(task.status.dataItems):
                text_content = getattr(di, "text", str(di))
                data_items_info += f"\n  [item{i}]: {truncate_text(text_content, 150)}"

        self.logger.info(
            "on_get response",
            task_id=task.taskId,
            state=task.status.state,
            products=products_info,
            products_preview=products_preview,
            data_items=data_items_info,
        )
        return await DefaultHandlers.get(command, task)

    async def on_cancel(self, command: TaskCommand, task: TaskResult) -> TaskResult:
        self._add_command(task.taskId, command)

        # Cancel running future if exists
        ctx = self.tasks.get(task.taskId)
        if ctx and ctx.running_future and not ctx.running_future.done():
            ctx.running_future.cancel()
            try:
                await ctx.running_future
            except asyncio.CancelledError:
                self.logger.info("Task background processing cancelled", task_id=task.taskId)
            except Exception:
                self.logger.exception("Error cancelling task", task_id=task.taskId)
            finally:
                ctx.running_future = None

        terminal_states = {
            TaskState.Completed,
            TaskState.Failed,
            TaskState.Rejected,
            TaskState.Canceled,
        }
        if task.status.state in terminal_states:
            return task
        return self._update_task_status(task.taskId, TaskState.Canceled)

    async def on_complete(self, command: TaskCommand, task: TaskResult) -> TaskResult:
        self._add_command(task.taskId, command)
        if task.status.state == TaskState.AwaitingCompletion:
            return self._update_task_status(task.taskId, TaskState.Completed)
        return task

    async def on_continue(self, command: TaskCommand, task: TaskResult) -> TaskResult:
        """
        处理 Continue 命令：用户提供了补充信息。

        根据 AIP 协议：
        - AwaitingInput + Continue → Working（重新进入 analysis 阶段）
        - AwaitingCompletion + Continue → Working（Leader 对产出物不满意，提供新数据）
        """
        task_id = task.taskId
        current_state = task.status.state
        input_text = self._extract_text(command)

        self.logger.info(
            "Continue command received",
            task_id=task_id,
            current_state=current_state,
            input_preview=input_text[:100],
        )

        self._add_command(task.taskId, command)
        if current_state not in (
            TaskState.AwaitingInput,
            TaskState.AwaitingCompletion,
        ):
            self.logger.warning("Continue ignored: invalid state", task_id=task_id, state=current_state)
            return task

        if not input_text.strip():
            self.logger.warning("Continue ignored: empty input", task_id=task_id)
            return task

        if current_state == TaskState.AwaitingInput:
            self.logger.info(
                "AwaitingInput -> Working, re-running analysis with new input",
                task_id=task_id,
            )
            self._update_task_status(task.taskId, TaskState.Working)
            future = asyncio.create_task(self._run_analysis_stage(task.taskId, command))
            self.tasks[task.taskId].running_future = future

        elif current_state == TaskState.AwaitingCompletion:
            self.logger.info(
                "AwaitingCompletion -> Working, re-running analysis with new input",
                task_id=task_id,
            )
            self._update_task_status(task.taskId, TaskState.Working)
            future = asyncio.create_task(self._run_analysis_stage(task.taskId, command))
            self.tasks[task.taskId].running_future = future

        return self.tasks[task.taskId].task

    async def dispatch(self, request: RpcRequest) -> RpcResponse:
        command = request.params.command
        task_id = getattr(command, "taskId", None)

        if not task_id:
            return RpcResponse(
                id=request.id,
                error=JSONRPCError(code=-32602, message="taskId is required"),
            )

        ctx = self.tasks.get(task_id)
        task = ctx.task if ctx else None

        # AIP v2: command 字段在 TaskCommand 上
        command_type = getattr(command, "command", None)

        try:
            if command_type == TaskCommandType.Start:
                result = await self.on_start(command, task)
            elif command_type == TaskCommandType.Get:
                if not task:
                    return RpcResponse(
                        id=request.id,
                        error=JSONRPCError(code=-32001, message="Task not found"),
                    )
                result = await self.on_get(command, task)
            elif command_type == TaskCommandType.Cancel:
                if not task:
                    return RpcResponse(
                        id=request.id,
                        error=JSONRPCError(code=-32001, message="Task not found"),
                    )
                result = await self.on_cancel(command, task)
            elif command_type == TaskCommandType.Complete:
                if not task:
                    return RpcResponse(
                        id=request.id,
                        error=JSONRPCError(code=-32001, message="Task not found"),
                    )
                result = await self.on_complete(command, task)
            elif command_type == TaskCommandType.Continue:
                if not task:
                    return RpcResponse(
                        id=request.id,
                        error=JSONRPCError(code=-32001, message="Task not found"),
                    )
                result = await self.on_continue(command, task)
            else:
                return RpcResponse(
                    id=request.id,
                    error=JSONRPCError(code=-32602, message=f"Unknown command type: {command_type}"),
                )

            return RpcResponse(id=request.id, result=result)

        except Exception as e:
            self.logger.exception("Dispatch error")
            if task_id and self.tasks.get(task_id):
                self._update_task_status(
                    task_id,
                    TaskState.Failed,
                    [TextDataItem(text=f"Internal error: {e!s}")],
                )
                return RpcResponse(id=request.id, result=self.tasks[task_id].task)

            return RpcResponse(
                id=request.id,
                error=JSONRPCError(code=-32603, message="Internal error", data=str(e)),
            )
