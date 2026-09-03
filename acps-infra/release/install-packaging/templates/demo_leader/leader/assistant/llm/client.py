"""
Leader Agent Platform - LLM Client

本模块封装 LLM API 调用逻辑，提供：
- 多 profile 支持（default, fast, pro 等）
- 结构化输出解析
- 错误重试与异常处理
- 调用日志记录
"""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from ..config import settings
from ..models import (
    LLM6_CALL_TIMEOUT_SECONDS,
    LLM_CALL_TIMEOUT_SECONDS,
    LLM_MAX_RETRIES,
)
from ..models.exceptions import LLMCallError, LLMParseError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """
    LLM API 客户端。

    支持多个 LLM profile，每个 profile 可以配置不同的模型、温度等参数。
    典型的 profile 包括：
    - llm.default: 默认配置，用于一般性任务
    - llm.fast: 快速模型，用于简单分类任务（如 LLM-1）
    - llm.pro: 高级模型，用于复杂推理任务
    """

    def __init__(self):
        """初始化 LLM 客户端。"""
        self._clients: dict[str, OpenAI] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._init_profiles()

    def _init_profiles(self) -> None:
        """从配置初始化所有 LLM profiles。"""
        llm_config = settings.get("llm", {})

        for profile_name, profile_data in llm_config.items():
            if not isinstance(profile_data, dict):
                continue

            full_name = f"llm.{profile_name}"
            self._profiles[full_name] = profile_data

            # llm.pro 用于 LLM-6 (Aggregation)，需要更长超时时间
            timeout = LLM6_CALL_TIMEOUT_SECONDS if profile_name == "pro" else LLM_CALL_TIMEOUT_SECONDS

            # 创建 OpenAI 客户端
            self._clients[full_name] = OpenAI(
                api_key=profile_data.get("api_key", ""),
                base_url=profile_data.get("base_url"),
                timeout=timeout,
                # Avoid nested SDK retries stacking with LLM_MAX_RETRIES
                # (each retry waits the full request timeout).
                max_retries=0,
            )

            logger.info(f"Initialized LLM profile: {full_name} -> {profile_data.get('model')} (timeout={timeout}s)")

    def get_profile(self, profile_name: str) -> dict[str, Any]:
        """
        获取指定 profile 的配置。

        Args:
            profile_name: profile 名称（如 "llm.default" 或 "llm.fast"）

        Returns:
            profile 配置字典

        Raises:
            LLMCallError: profile 不存在
        """
        # 处理简写形式
        if not profile_name.startswith("llm."):
            profile_name = f"llm.{profile_name}"

        if profile_name not in self._profiles:
            raise LLMCallError(f"Unknown LLM profile: {profile_name}")

        return self._profiles[profile_name]

    def call(
        self,
        profile_name: str,
        system_prompt: str,
        user_message: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        调用 LLM 获取文本响应。

        Args:
            profile_name: LLM profile 名称
            system_prompt: 系统提示词
            user_message: 用户消息
            temperature: 覆盖默认温度（可选）
            max_tokens: 最大 token 数（可选）

        Returns:
            LLM 的响应文本

        Raises:
            LLMCallError: API 调用失败
        """
        # 规范化 profile 名称
        if not profile_name.startswith("llm."):
            profile_name = f"llm.{profile_name}"

        if profile_name not in self._clients:
            raise LLMCallError(f"Unknown LLM profile: {profile_name}")

        client = self._clients[profile_name]
        profile = self._profiles[profile_name]

        # 准备参数
        model = profile.get("model", "gpt-4")
        temp = temperature if temperature is not None else profile.get("temperature", 0.7)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 重试逻辑
        last_error = None
        for attempt in range(LLM_MAX_RETRIES):
            try:
                logger.debug(f"LLM call [{profile_name}] attempt {attempt + 1}: model={model}, temp={temp}")

                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": temp,
                }
                # Reasoning models (e.g. deepseek-v4-pro-guan) need headroom so
                # content is not starved by reasoning_content.
                effective_max = max_tokens
                if effective_max is None and profile_name in {"llm.default", "llm.pro"}:
                    effective_max = 4096
                if effective_max:
                    kwargs["max_tokens"] = effective_max

                # Gateways (deepseek/doubao) may enable thinking by default and hang
                # on medium+ prompts; always disable so content returns promptly.
                model_l = (model or "").lower()
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

                # OpenAI SDK/httpx can hang inside the long-lived demo_leader process;
                # urllib is reliable for all models here.
                content = self._call_via_urllib(
                    profile=profile,
                    model=model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=effective_max,
                    timeout=float(LLM_CALL_TIMEOUT_SECONDS),
                    extra_body=kwargs.get("extra_body"),
                )

                logger.debug(f"LLM response received: {len(content)} chars")
                return content

            except Exception as e:
                last_error = e
                logger.warning(f"LLM call failed (attempt {attempt + 1}/{LLM_MAX_RETRIES}): {e}")

        raise LLMCallError(f"LLM call failed after {LLM_MAX_RETRIES} retries: {last_error}")

    def _call_via_urllib(
        self,
        profile: dict[str, Any],
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int | None,
        timeout: float,
        extra_body: dict[str, Any] | None = None,
    ) -> str:
        """Direct HTTPS chat.completions call (avoids SDK/httpx hangs on some gateways)."""
        base_url = (profile.get("base_url") or "").rstrip("/") + "/"
        api_key = profile.get("api_key", "")
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if extra_body:
            body.update(extra_body)

        req = urllib.request.Request(
            base_url + "chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise LLMCallError(f"urllib HTTP {e.code}: {detail}") from e
        except Exception as e:
            raise LLMCallError(f"urllib request failed: {e}") from e

        message = (payload.get("choices") or [{}])[0].get("message") or {}
        content = message.get("content") or ""
        if not content:
            content = message.get("reasoning_content") or ""
        return content

    def call_structured(
        self,
        profile_name: str,
        system_prompt: str,
        user_message: str,
        response_model: type[T],
        temperature: float | None = None,
        parse_retry_count: int = 0,
        max_tokens: int | None = None,
    ) -> T:
        """
        调用 LLM 并解析为结构化输出。

        使用 JSON 模式请求 LLM 返回符合指定 Pydantic 模型的响应。

        Args:
            profile_name: LLM profile 名称
            system_prompt: 系统提示词（应包含 JSON 格式要求）
            user_message: 用户消息
            response_model: 期望的响应 Pydantic 模型类
            temperature: 覆盖默认温度（可选）

        Returns:
            解析后的 Pydantic 模型实例

        Raises:
            LLMCallError: API 调用失败
            LLMParseError: 响应解析失败
        """
        current_user_message = user_message
        total_attempts = max(1, parse_retry_count + 1)
        last_error: LLMParseError | None = None

        for attempt in range(1, total_attempts + 1):
            raw_response = self.call(
                profile_name=profile_name,
                system_prompt=system_prompt,
                user_message=current_user_message,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            try:
                return self._parse_structured_response(raw_response, response_model)
            except LLMParseError as exc:
                last_error = exc
                if attempt >= total_attempts:
                    raise

                logger.warning(
                    "Structured LLM response parse failed for %s (attempt %s/%s): %s",
                    response_model.__name__,
                    attempt,
                    total_attempts,
                    exc,
                )
                current_user_message = self._build_structured_retry_message(
                    user_message=user_message,
                    response_model=response_model,
                    parse_error=exc,
                    attempt=attempt,
                )

        if last_error is not None:
            raise last_error
        raise LLMParseError("Structured LLM call failed without parse result")

    def _parse_structured_response(
        self,
        raw_response: str,
        response_model: type[T],
    ) -> T:
        """提取并验证结构化 JSON 响应。"""
        json_str = self._extract_json(raw_response)
        if not json_str:
            raise LLMParseError(f"No JSON found in LLM response: {raw_response[:200]}...")

        try:
            data = json.loads(json_str)
            return response_model.model_validate(data)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error at position {e.pos}: {e.msg}")
            logger.error(f"Extracted JSON (first 500 chars): {json_str[:500]}...")
            raise LLMParseError(f"Invalid JSON in LLM response: {e}")
        except ValidationError as e:
            raise LLMParseError(f"Response validation failed: {e}")

    def _build_structured_retry_message(
        self,
        user_message: str,
        response_model: type[T],
        parse_error: LLMParseError,
        attempt: int,
    ) -> str:
        """在结构化解析失败后追加更严格的 JSON 重试约束。"""
        retry_note = (
            "\n\n"
            "【系统重试提示】你上一条回复未通过系统 JSON 解析，请重新完整输出。\n"
            f"- 当前是结构化输出重试第 {attempt} 次\n"
            f"- 目标响应模型：{response_model.__name__}\n"
            f"- 上一次错误：{parse_error}\n"
            "- 只能输出一个合法 JSON 对象\n"
            "- 不要输出 Markdown 代码块、解释文字、注释或省略号\n"
            "- 所有键名和字符串必须使用双引号\n"
            "- 不要出现尾逗号，字段结构必须与前述要求完全一致"
        )
        return f"{user_message.rstrip()}{retry_note}"

    def _extract_json(self, text: str) -> str | None:
        """
        从 LLM 响应中提取 JSON。

        支持以下格式：
        1. 纯 JSON 响应
        2. ```json ... ``` 代码块
        3. ``` ... ``` 代码块
        4. 文本中嵌入的 JSON 对象

        Args:
            text: LLM 响应文本

        Returns:
            提取的 JSON 字符串或 None
        """
        text = text.strip()

        # 1. 尝试直接解析
        if text.startswith("{") and text.endswith("}"):
            json_str = self._extract_first_json_object(text)
            if json_str:
                return json_str

        # 2. 尝试 ```json ... ``` 格式
        json_block_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if json_block_match:
            json_str = self._extract_first_json_object(json_block_match.group(1).strip())
            if json_str:
                return json_str

        # 3. 尝试 ``` ... ``` 格式
        code_block_match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
        if code_block_match:
            content = code_block_match.group(1).strip()
            if content.startswith("{"):
                json_str = self._extract_first_json_object(content)
                if json_str:
                    return json_str

        # 4. 尝试提取文本中的 JSON 对象
        return self._extract_first_json_object(text)

    def _extract_first_json_object(self, text: str) -> str | None:
        """提取文本中第一个花括号平衡且可解析的 JSON 对象。"""
        start = text.find("{")
        while start != -1:
            depth = 0
            in_string = False
            is_escaped = False

            for index in range(start, len(text)):
                char = text[index]

                if in_string:
                    if is_escaped:
                        is_escaped = False
                    elif char == "\\":
                        is_escaped = True
                    elif char == '"':
                        in_string = False
                    continue

                if char == '"':
                    in_string = True
                    continue

                if char == "{":
                    depth += 1
                    continue

                if char != "}":
                    continue

                depth -= 1
                if depth != 0:
                    continue

                candidate = text[start : index + 1]
                try:
                    json.loads(candidate)
                except json.JSONDecodeError:
                    break
                return candidate

            start = text.find("{", start + 1)

        return None


# 模块级单例
_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
