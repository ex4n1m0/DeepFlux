"""Swappable LLM client with tool-calling support."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

import requests

from config import LLMConfig, LLM_PROVIDER_PRESETS

logger = logging.getLogger(__name__)

# Called with (kind, text) as tokens stream in; kind is "content" or "reasoning".
DeltaCallback = Callable[[str, str], None]

# Transient failures worth retrying (HTTP status codes); 4xx other than 429
# are client errors and fail immediately.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


def _default_save_path() -> str:
    """Cross-platform default download directory for the demo LLM client."""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "deeptorrent")


class LLMMessage:
    def __init__(
        self,
        role: str,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        reasoning: Optional[str] = None,
    ) -> None:
        self.role = role
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning = reasoning


class LLMClient(ABC):
    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        on_delta: Optional[DeltaCallback] = None,
    ) -> LLMMessage:
        ...

    @abstractmethod
    def supports_tools(self) -> bool:
        ...


class DeepSeekClient(LLMClient):
    """OpenAI-compatible chat-completions client.

    Despite the name this talks to any provider in LLM_PROVIDER_PRESETS
    (DeepSeek, OpenRouter, custom endpoints); DeepSeek-specific behavior
    (the `reasoning_effort` payload field) is gated on the provider.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.provider = (config.provider or "deepseek").lower()
        preset = LLM_PROVIDER_PRESETS.get(self.provider, {})
        self.capabilities = preset
        if self.provider == "deepseek":
            self.api_key = config.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        else:
            self.api_key = config.api_key
        preset_models = preset.get("models") or []
        if self.provider == "custom" and not config.base_url.strip():
            raise ValueError("Custom LLM provider requires a base URL")
        self.base_url = (config.base_url or preset.get("base_url") or "https://api.deepseek.com").rstrip("/")
        model = config.model or (preset_models[0] if preset_models else "deepseek-flash")
        # OpenRouter needs vendor-prefixed model slugs. The config stores bare
        # DeepSeek names — normalise here so switching endpoints Just Works.
        # The V4.1-Flash slug is versioned on OpenRouter, so a bare name is
        # never prefixed blindly (deepseek/deepseek-flash does not exist).
        _openrouter_slugs = {
            "deepseek-flash": "deepseek/deepseek-v4.1-flash",
            "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
        }
        if self.provider == "openrouter" and "/" not in model:
            model = _openrouter_slugs.get(model) or f"deepseek/{model}"
        self.model = model
        self.reasoning_effort = config.reasoning_effort or "high"
        self.send_reasoning_effort = bool(
            config.custom_reasoning_effort if self.provider == "custom"
            else preset.get("reasoning_effort", False)
        )

    def supports_tools(self) -> bool:
        return bool(self.capabilities.get("tools", True))

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        on_delta: Optional[DeltaCallback] = None,
    ) -> LLMMessage:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        # Provider capability metadata decides whether and how reasoning effort is sent.
        if self.send_reasoning_effort:
            _effort = effort or self.reasoning_effort
            _effort = self.capabilities.get("effort_map", {}).get(_effort, _effort)
            payload["reasoning_effort"] = _effort
        if tools and self.supports_tools():
            payload["tools"] = tools
        use_stream = on_delta is not None and bool(self.capabilities.get("streaming", True))
        if use_stream:
            payload["stream"] = True

        response = self._post_with_retry(f"{self.base_url}/chat/completions", headers, payload)
        if use_stream:
            return self._consume_stream(response, on_delta)

        data = response.json()
        message = data["choices"][0]["message"]
        return LLMMessage(
            role=message.get("role", "assistant"),
            content=message.get("content"),
            tool_calls=message.get("tool_calls", []),
            # OpenRouter exposes thinking as `reasoning` instead of DeepSeek's
            # `reasoning_content`.
            reasoning=message.get("reasoning_content") or message.get("reasoning"),
        )

    def _post_with_retry(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> requests.Response:
        """POST with exponential backoff on transient network/5xx/429 failures.

        Only the request establishment is retried — once a streaming response
        body starts arriving, errors propagate instead of duplicating tokens.
        """
        last_exc: Optional[Exception] = None
        want_stream = bool(payload.get("stream"))
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=120, stream=want_stream)
                if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                    wait = (2 ** (attempt - 1)) + random.random()
                    logger.warning("DeepSeek API %s (attempt %d/%d) — retrying in %.1fs",
                                   response.status_code, attempt, _MAX_ATTEMPTS, wait)
                    response.close()
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt >= _MAX_ATTEMPTS:
                    break
                wait = (2 ** (attempt - 1)) + random.random()
                logger.warning("DeepSeek request failed (attempt %d/%d): %s — retrying in %.1fs",
                               attempt, _MAX_ATTEMPTS, exc, wait)
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _consume_stream(response: requests.Response, on_delta: DeltaCallback) -> LLMMessage:
        """Assemble a full message from SSE chunks, forwarding deltas live."""
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_acc: Dict[int, Dict[str, Any]] = {}
        try:
            # SSE responses often omit charset; requests then defaults to
            # ISO-8859-1 and mangles multi-byte UTF-8 (emoji, CJK) — force it.
            response.encoding = "utf-8"
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    on_delta("content", text)
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    on_delta("reasoning", reasoning)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
        finally:
            response.close()
        return LLMMessage(
            role="assistant",
            content="".join(content_parts) or None,
            tool_calls=[tool_acc[i] for i in sorted(tool_acc)],
            reasoning="".join(reasoning_parts) or None,
        )


class DummyLLMClient(LLMClient):
    """Simple pattern-matching fallback for demos and tests without an API key."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def supports_tools(self) -> bool:
        return True

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        effort: Optional[str] = None,
        model: Optional[str] = None,
        on_delta: Optional[DeltaCallback] = None,
    ) -> LLMMessage:
        user = ""
        last_user_idx = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user" and m.get("content"):
                user = m["content"].lower()
                last_user_idx = i

        # If a tool has already been executed for this user message, summarize.
        tool_after_user = last_user_idx >= 0 and any(m.get("role") == "tool" for m in messages[last_user_idx + 1 :])
        size_match = re.search(r"pause everything over (\d+)\s*gb", user)
        tool_names = {
            tc.get("function", {}).get("name", "")
            for message in messages[last_user_idx + 1:]
            if message.get("role") == "assistant"
            for tc in message.get("tool_calls", [])
        }
        if tool_after_user and size_match and "pause_torrent" not in tool_names:
            threshold = int(size_match.group(1)) * 1024 * 1024 * 1024
            torrents = []
            for message in messages[last_user_idx + 1:]:
                if message.get("role") != "tool":
                    continue
                try:
                    result = json.loads(message.get("content") or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(result.get("torrents"), list):
                    torrents = result["torrents"]
                    break
            pause_calls = [
                {
                    "id": f"call_pause_{index}",
                    "type": "function",
                    "function": {
                        "name": "pause_torrent",
                        "arguments": json.dumps({"info_hash": torrent["info_hash"]}),
                    },
                }
                for index, torrent in enumerate(torrents)
                if torrent.get("total_size", 0) > threshold and torrent.get("info_hash")
            ]
            if pause_calls:
                return LLMMessage(role="assistant", content=None, tool_calls=pause_calls)
            return LLMMessage(role="assistant", content="No torrents exceed that size.", tool_calls=[])
        if tool_after_user:
            if "list" in user or "show" in user or "status" in user:
                return LLMMessage(role="assistant", content="Here are the current torrents.", tool_calls=[])
            if "magnet" in user:
                return LLMMessage(role="assistant", content="The magnet link has been added.", tool_calls=[])
            if ".torrent" in user or "add" in user:
                return LLMMessage(role="assistant", content="The torrent file has been added.", tool_calls=[])
            if "pause" in user:
                return LLMMessage(role="assistant", content="Matching torrents have been paused.", tool_calls=[])
            return LLMMessage(role="assistant", content="Done.", tool_calls=[])

        # Add torrent file
        file_match = re.search(r"(?:add\s+)?(.+\.torrent)(?:\s+to\s+(\w+))", user)
        if file_match and not user.startswith("magnet"):
            path = file_match.group(1).strip()
            category = (file_match.group(2) or "Other").capitalize()
            return LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_add_torrent_file",
                        "type": "function",
                        "function": {
                            "name": "add_torrent_file",
                            "arguments": json.dumps({
                                "path": os.path.abspath(path),
                                "save_path": _default_save_path(),
                                "category": category,
                            }),
                        },
                    }
                ],
            )

        # Add magnet
        magnet_match = re.search(r"magnet:\?xt=urn:btih:([a-f0-9]{40})", user)
        if magnet_match:
            category = "Other"
            for c in ["movies", "tv", "software"]:
                if c in user:
                    category = c.capitalize()
            return LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_add_magnet",
                        "type": "function",
                        "function": {
                            "name": "add_magnet",
                            "arguments": json.dumps({
                                "uri": f"magnet:?xt=urn:btih:{magnet_match.group(1)}",
                                "save_path": _default_save_path(),
                                "category": category,
                            }),
                        },
                    }
                ],
            )

        # Pause everything over N GB
        size_match = re.search(r"pause everything over (\d+)\s*gb", user)
        if size_match:
            return LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_list_torrents",
                        "type": "function",
                        "function": {
                            "name": "list_torrents",
                            "arguments": json.dumps({}),
                        },
                    }
                ],
            )

        # List torrents
        if any(phrase in user for phrase in ("list", "show", "status")):
            return LLMMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_list_torrents",
                        "type": "function",
                        "function": {
                            "name": "list_torrents",
                            "arguments": json.dumps({}),
                        },
                    }
                ],
            )

        # Offline demo mode can't parse this — point the user at the fix.
        return LLMMessage(
            role="assistant",
            content="Add an LLM API key (DeepSeek, OpenRouter, …) in File → API Keys to make the agent work.",
            tool_calls=[],
        )


def create_llm_client(config: LLMConfig) -> LLMClient:
    provider = (config.provider or "").lower()
    if provider in LLM_PROVIDER_PRESETS:
        return DeepSeekClient(config)
    if provider == "dummy":
        return DummyLLMClient(config)
    raise ValueError(f"Unknown LLM provider: {config.provider}")
