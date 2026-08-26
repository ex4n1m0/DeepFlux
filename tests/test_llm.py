"""Tests for the DeepSeek client: retries, reasoning capture, streaming."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from agent.llm import DeepSeekClient, DummyLLMClient, create_llm_client
from config import LLMConfig


def _client() -> DeepSeekClient:
    return DeepSeekClient(LLMConfig(api_key="sk-test"))


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json.return_value = payload
    return resp


def _chat_payload(content: str = "hi", reasoning: str | None = None) -> dict:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message}]}


@patch("agent.llm.time.sleep")
@patch("agent.llm.requests.post")
def test_retry_on_server_error_then_success(mock_post, mock_sleep):
    fail = MagicMock()
    fail.status_code = 500
    mock_post.side_effect = [fail, _ok_response(_chat_payload("hello"))]

    msg = _client().chat([{"role": "user", "content": "hi"}])

    assert msg.content == "hello"
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()


@patch("agent.llm.time.sleep")
@patch("agent.llm.requests.post")
def test_retry_exhausted_raises(mock_post, mock_sleep):
    fail = MagicMock()
    fail.status_code = 500
    fail.raise_for_status.side_effect = requests.HTTPError("500")
    mock_post.return_value = fail

    with pytest.raises(requests.HTTPError):
        _client().chat([{"role": "user", "content": "hi"}])
    assert mock_post.call_count == 3  # initial + 2 retries


@patch("agent.llm.time.sleep")
@patch("agent.llm.requests.post")
def test_client_error_not_retried(mock_post, mock_sleep):
    fail = MagicMock()
    fail.status_code = 401
    fail.raise_for_status.side_effect = requests.HTTPError("401")
    mock_post.return_value = fail

    with pytest.raises(requests.HTTPError):
        _client().chat([{"role": "user", "content": "hi"}])
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("agent.llm.time.sleep")
@patch("agent.llm.requests.post")
def test_connection_error_retried(mock_post, mock_sleep):
    mock_post.side_effect = [
        requests.ConnectionError("down"),
        _ok_response(_chat_payload("back")),
    ]
    msg = _client().chat([{"role": "user", "content": "hi"}])
    assert msg.content == "back"
    assert mock_post.call_count == 2


@patch("agent.llm.requests.post")
def test_reasoning_content_captured(mock_post):
    mock_post.return_value = _ok_response(_chat_payload("answer", reasoning="thoughts"))
    msg = _client().chat([{"role": "user", "content": "hi"}])
    assert msg.content == "answer"
    assert msg.reasoning == "thoughts"


@patch("agent.llm.requests.post")
def test_per_call_model_and_effort_override(mock_post):
    mock_post.return_value = _ok_response(_chat_payload())
    _client().chat(
        [{"role": "user", "content": "hi"}],
        model="deepseek-v4-flash",
        effort="low",
    )
    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["reasoning_effort"] == "low"
    assert "stream" not in payload  # non-streaming without on_delta


@patch("agent.llm.requests.post")
def test_streaming_assembles_message_and_tool_calls(mock_post):
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"list_","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"torrents","arguments":"{}"}}]}}]}',
        "data: [DONE]",
    ]
    resp = _ok_response({})
    resp.iter_lines.return_value = iter(lines)
    mock_post.return_value = resp

    deltas = []
    msg = _client().chat(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda kind, text: deltas.append((kind, text)),
    )

    assert msg.content == "Hello world"
    assert msg.reasoning == "thinking"
    assert msg.tool_calls[0]["id"] == "call_1"
    assert msg.tool_calls[0]["function"]["name"] == "list_torrents"
    assert msg.tool_calls[0]["function"]["arguments"] == "{}"
    assert ("content", "Hello") in deltas
    assert ("reasoning", "thinking") in deltas
    assert mock_post.call_args.kwargs["json"]["stream"] is True


# ---------------------------------------------------------------------------
# Provider presets — both DeepSeek direct and OpenRouter send reasoning_effort
# ---------------------------------------------------------------------------

@patch("agent.llm.requests.post")
def test_openrouter_sends_reasoning_effort_normalized(mock_post):
    mock_post.return_value = _ok_response(_chat_payload())
    client = DeepSeekClient(LLMConfig(provider="openrouter", api_key="sk-or-test"))
    client.chat([{"role": "user", "content": "hi"}], effort="low")
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert payload["model"] == "deepseek/deepseek-v4-pro"
    assert payload["reasoning_effort"] == "low"  # OpenRouter supports this natively

@patch("agent.llm.requests.post")
def test_openrouter_maps_max_to_xhigh(mock_post):
    """OpenRouter calls max-reasoning 'xhigh' — map it so the model gets full effort."""
    mock_post.return_value = _ok_response(_chat_payload())
    client = DeepSeekClient(LLMConfig(
        provider="openrouter", api_key="sk-or-test", reasoning_effort="max",
    ))
    client.chat([{"role": "user", "content": "hi"}])
    payload = mock_post.call_args.kwargs["json"]
    assert payload["reasoning_effort"] == "xhigh"


@patch("agent.llm.requests.post")
def test_deepseek_sends_reasoning_effort_and_default_url(mock_post):
    mock_post.return_value = _ok_response(_chat_payload())
    _client().chat([{"role": "user", "content": "hi"}])
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "https://api.deepseek.com/chat/completions"
    assert payload["reasoning_effort"] == "high"


@patch("agent.llm.requests.post")
def test_explicit_base_url_and_model_override_preset(mock_post):
    mock_post.return_value = _ok_response(_chat_payload())
    client = DeepSeekClient(LLMConfig(
        provider="deepseek", api_key="k",
        base_url="http://localhost:1234/v1", model="local-model",
    ))
    client.chat([{"role": "user", "content": "hi"}])
    url = mock_post.call_args.args[0]
    payload = mock_post.call_args.kwargs["json"]
    assert url == "http://localhost:1234/v1/chat/completions"
    assert payload["model"] == "local-model"
    # reasoning_effort is sent because provider is deepseek (even with custom base_url)
    assert payload["reasoning_effort"] == "high"


@patch("agent.llm.requests.post")
def test_openrouter_reasoning_field_captured(mock_post):
    message = {"role": "assistant", "content": "answer", "reasoning": "or-thoughts"}
    mock_post.return_value = _ok_response({"choices": [{"message": message}]})
    client = DeepSeekClient(LLMConfig(provider="openrouter", api_key="sk-or-test"))
    msg = client.chat([{"role": "user", "content": "hi"}])
    assert msg.reasoning == "or-thoughts"


def test_deepseek_env_key_not_used_for_other_providers(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert DeepSeekClient(LLMConfig(provider="deepseek")).api_key == "sk-env"
    assert DeepSeekClient(LLMConfig(provider="openrouter")).api_key == ""


def test_create_llm_client_providers():
    assert isinstance(create_llm_client(LLMConfig(provider="deepseek")), DeepSeekClient)
    assert isinstance(create_llm_client(LLMConfig(provider="openrouter")), DeepSeekClient)
    assert isinstance(create_llm_client(LLMConfig(provider="dummy")), DummyLLMClient)
    with pytest.raises(ValueError):
        create_llm_client(LLMConfig(provider="bogus"))
