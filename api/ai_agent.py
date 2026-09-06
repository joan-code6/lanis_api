"""Small OpenRouter-compatible tool-calling agent used by WhatsApp.

The model can only request explicitly registered tools. Tool execution stays in
LANIS so the model never receives credentials or arbitrary backend access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger("ai_agent")

MAX_AGENT_ROUNDS = 20
MAX_AGENT_TOOL_CALLS = 32
MAX_TOOL_RESULT_CHARS = 60_000
MAX_AGENT_CONTEXT_CHARS = 240_000


@dataclass(frozen=True)
class AIConfig:
    endpoint: str
    api_key: str
    default_model: str

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            endpoint=os.getenv("ai_endpoint", "").strip(),
            api_key=os.getenv("ai_api_key", "").strip(),
            default_model=os.getenv("ai_default_model", "").strip(),
        )

    @property
    def configured(self) -> bool:
        parsed = urlparse(self.endpoint)
        return bool(
            self.api_key
            and self.default_model
            and parsed.scheme == "https"
            and parsed.netloc
            and parsed.path.rstrip("/").endswith("/chat/completions")
        )


ToolHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler

    def api_definition(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class AIProviderError(RuntimeError):
    """The configured model endpoint failed or returned an invalid response."""


class OpenRouterClient:
    def __init__(self, config: AIConfig):
        if not config.configured:
            raise AIProviderError("AI is not configured")
        self.config = config

    async def complete(
        self, messages: List[Dict[str, Any]], tools: List[AgentTool]
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.config.default_model,
            "messages": messages,
            "tools": [tool.api_definition() for tool in tools],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"enabled": True, "exclude": False},
            "max_completion_tokens": 32_000,
            "provider": {
                "allow_fallbacks": True,
                "data_collection": "deny",
                "zdr": True,
            },
        }

        def _request() -> Dict[str, Any]:
            try:
                response = requests.post(
                    self.config.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://lanis.arg-server.de",
                        "X-OpenRouter-Title": "LANIS WhatsApp Assistant",
                    },
                    json=payload,
                    timeout=(5, 75),
                    allow_redirects=False,
                )
            except requests.RequestException as error:
                raise AIProviderError("AI endpoint request failed") from error
            if response.is_redirect:
                raise AIProviderError("AI endpoint attempted a redirect")
            if response.status_code >= 400:
                raise AIProviderError(
                    f"AI endpoint returned HTTP {response.status_code}"
                )
            try:
                value = response.json()
            except ValueError as error:
                raise AIProviderError("AI endpoint returned invalid JSON") from error
            if not isinstance(value, dict):
                raise AIProviderError("AI endpoint returned an invalid response")
            if isinstance(value.get("error"), dict):
                code = value["error"].get("code") or "unknown"
                raise AIProviderError(f"AI endpoint returned error {code}")
            return value

        result = await run_in_threadpool(_request)
        choices = result.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
        ):
            raise AIProviderError("AI response contained no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise AIProviderError("AI response contained no assistant message")
        return message


def _assistant_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Retain the fields required for provider-neutral interleaved reasoning."""
    allowed = {
        "role",
        "content",
        "tool_calls",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
    }
    cleaned = {key: value for key, value in message.items() if key in allowed}
    cleaned["role"] = "assistant"
    return cleaned


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _tool_result(value: Any) -> str:
    try:
        result = json.dumps(
            value, ensure_ascii=False, default=str, separators=(",", ":")
        )
    except (TypeError, ValueError):
        result = json.dumps(
            {"success": False, "error": "Tool result was not serializable"}
        )
    if len(result) > MAX_TOOL_RESULT_CHARS:
        return json.dumps(
            {
                "success": False,
                "error": "Tool result was too large; narrow the request and try again",
            },
            ensure_ascii=False,
        )
    return result


async def run_agent(
    *,
    config: AIConfig,
    system_prompt: str,
    user_message: str,
    tools: List[AgentTool],
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Run an interleaved reasoning/tool loop until the model returns text."""
    client = OpenRouterClient(config)
    tool_map = {tool.name: tool for tool in tools}
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_message},
    ]
    total_tool_calls = 0
    repeated_calls: Dict[str, int] = {}

    for _round in range(MAX_AGENT_ROUNDS):
        message = await client.complete(messages, tools)
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise AIProviderError("AI response contained invalid tool calls")
        calls = [
            call
            for call in raw_calls
            if isinstance(call, dict)
            and isinstance(call.get("id"), str)
            and call["id"].strip()
            and isinstance(call.get("function"), dict)
        ]
        if raw_calls and not calls:
            raise AIProviderError("AI response contained invalid tool calls")
        assistant_message = _assistant_message(message)
        if raw_calls:
            assistant_message["tool_calls"] = calls
        messages.append(assistant_message)
        if not calls:
            text = _text_content(message.get("content"))
            if text:
                return text
            raise AIProviderError("AI returned neither text nor tool calls")

        if total_tool_calls + len(calls) > MAX_AGENT_TOOL_CALLS:
            raise AIProviderError("AI exceeded the tool-call limit")
        total_tool_calls += len(calls)

        async def execute(call: Dict[str, Any]) -> Dict[str, Any]:
            call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                result: Any = {"success": False, "error": "Invalid JSON arguments"}
            else:
                signature = (
                    f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
                )
                repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
                tool = tool_map.get(name)
                if tool is None:
                    result = {"success": False, "error": "Unknown or unauthorized tool"}
                elif repeated_calls[signature] > 2:
                    result = {
                        "success": False,
                        "error": "Identical tool call repeated; use existing results",
                    }
                else:
                    try:
                        result = await tool.handler(arguments)
                    except Exception:
                        logger.warning("AI tool %s failed", name, exc_info=True)
                        result = {"success": False, "error": "LANIS tool failed"}
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_result(result),
            }

        tool_messages = await asyncio.gather(*(execute(call) for call in calls))
        messages.extend(tool_messages)
        serialized_context = json.dumps(
            messages, ensure_ascii=False, default=str, separators=(",", ":")
        )
        if len(serialized_context) > MAX_AGENT_CONTEXT_CHARS:
            raise AIProviderError("AI conversation exceeded the context safety limit")

    raise AIProviderError("AI exceeded the reasoning-round limit")
