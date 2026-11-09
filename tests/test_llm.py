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
def test_local_provider_waits_out_model_load(mock_post, mock_sleep, monkeypatch):
    """The local server binds its port only after the model loads (8-60+s
    cold). Connection refusals during that window must be retried FAR
    longer than the cloud 3-attempt window, not surfaced as an error.
    (A model IS installed and the server is not manually off — otherwise
    the 2026-11-09 fast-fail path correctly bails on refusal #1.)"""
    import infra.local_model as lm
    monkeypatch.setattr(lm, "server_manually_off", lambda: False)
    monkeypatch.setattr(lm, "is_running", lambda timeout=2.5: False)
    monkeypatch.setattr(lm, "installed_models", lambda: [{"id": "m"}])
    ok = _ok_response(_chat_payload("finally"))
    refusals = [requests.ConnectionError("refused")] * 6
    mock_post.side_effect = refusals + [ok]

    client = DeepSeekClient(LLMConfig(provider="local"))
    msg = client.chat([{"role": "user", "content": "hi"}])

    assert msg.content == "finally"
    assert mock_post.call_count == 7  # 6 refusals waited out + success


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
        model="deepseek-flash",
        effort="low",
    )
    payload = mock_post.call_args.kwargs["json"]
    assert payload["model"] == "deepseek-flash"
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
    assert payload["model"] == "deepseek/deepseek-v4.1-flash"
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


@patch("agent.llm.requests.post")
def test_custom_provider_omits_reasoning_effort_and_blank_authorization(mock_post):
    mock_post.return_value = _ok_response(_chat_payload())
    client = DeepSeekClient(LLMConfig(
        provider="custom", api_key="", base_url="http://localhost:1234/v1/", model="local-model",
    ))

    client.chat([{"role": "user", "content": "hi"}])

    payload = mock_post.call_args.kwargs["json"]
    headers = mock_post.call_args.kwargs["headers"]
    assert mock_post.call_args.args[0] == "http://localhost:1234/v1/chat/completions"
    assert "reasoning_effort" not in payload
    assert "Authorization" not in headers


@patch("agent.llm.requests.post")
def test_custom_provider_can_opt_into_reasoning_effort(mock_post):
    mock_post.return_value = _ok_response(_chat_payload())
    client = DeepSeekClient(LLMConfig(
        provider="custom", api_key="key", base_url="https://llm.example/v1",
        model="reasoning-model", custom_reasoning_effort=True,
    ))

    client.chat([{"role": "user", "content": "hi"}], effort="low")

    assert mock_post.call_args.kwargs["json"]["reasoning_effort"] == "low"


def test_custom_provider_requires_base_url():
    with pytest.raises(ValueError, match="base URL"):
        DeepSeekClient(LLMConfig(provider="custom", base_url=""))


def test_create_llm_client_providers():
    assert isinstance(create_llm_client(LLMConfig(provider="deepseek")), DeepSeekClient)
    assert isinstance(create_llm_client(LLMConfig(provider="openrouter")), DeepSeekClient)
    assert isinstance(create_llm_client(LLMConfig(provider="custom", base_url="http://localhost:1234/v1", model="local")), DeepSeekClient)
    assert isinstance(create_llm_client(LLMConfig(provider="local")), DeepSeekClient)
    assert isinstance(create_llm_client(LLMConfig(provider="dummy")), DummyLLMClient)
    with pytest.raises(ValueError):
        create_llm_client(LLMConfig(provider="bogus"))


@patch("agent.llm.requests.post")
def test_stream_error_event_raises_instead_of_empty_reply(mock_post):
    """HTTP 200 + an error EVENT mid-stream (OpenRouter/DeepSeek under load)
    must raise so the loop reports it — the old code swallowed it and the
    agent answered with an empty bubble, no error, no retry."""
    lines = [
        'data: {"choices":[{"delta":{"content":"partial "}}]}',
        'data: {"error":{"message":"provider overloaded"}}',
        "data: [DONE]",
    ]
    resp = _ok_response({})
    resp.iter_lines.return_value = iter(lines)
    mock_post.return_value = resp

    with pytest.raises(RuntimeError, match="provider overloaded"):
        _client().chat([{"role": "user", "content": "hi"}],
                       on_delta=lambda kind, text: None)


@patch("agent.llm.requests.post")
def test_http_error_includes_server_body(mock_post):
    """llama-server rejects over-context requests with a 400 whose BODY is the
    actual reason — the client must surface it, not a bare "400 Bad Request"."""

    class _ErrResp:
        status_code = 400
        text = ('{"error": {"message": "request (40000 tokens) exceeds the available '
                'context size (32768 tokens)", "type": "exceed_context_size_error"}}')

        def raise_for_status(self):
            raise requests.HTTPError(
                "400 Client Error: Bad Request for url: http://127.0.0.1:8199/v1/chat/completions")

        def close(self):
            pass

    mock_post.return_value = _ErrResp()
    client = DeepSeekClient(LLMConfig(provider="local"))

    with pytest.raises(requests.HTTPError) as excinfo:
        client.chat([{"role": "user", "content": "hi"}])

    assert "32768" in str(excinfo.value)
    assert "available context size" in str(excinfo.value)


def test_local_provider_fails_fast_when_server_impossible(monkeypatch):
    """Regression (2026-11-09 review): with provider=local and the server
    OFF (or no model installed), every chat used to burn the full
    ~3.5-minute retry window before surfacing ConnectionError. The retry
    loop must bail on the FIRST refusal when the server can never answer."""
    import infra.local_model as lm
    from agent.llm import DeepSeekClient
    from config import LLM_PROVIDER_PRESETS

    calls = {"n": 0}

    def refused(url, **kw):
        calls["n"] += 1
        raise __import__("requests").ConnectionError("refused")

    monkeypatch.setattr("agent.llm.requests.post", refused)
    monkeypatch.setattr("agent.llm.time.sleep", lambda s: None)
    monkeypatch.setattr(lm, "server_manually_off", lambda: True)
    monkeypatch.setattr(lm, "is_running", lambda timeout=2.5: False)
    monkeypatch.setattr(lm, "installed_models", lambda: [])
    from config import LLMConfig
    client = DeepSeekClient(LLMConfig(provider="local"))
    import pytest as _pytest
    import requests as _rq
    with _pytest.raises(_rq.ConnectionError):
        client._post_with_retry("http://127.0.0.1:8199/v1/chat/completions",
                                 {}, {"stream": False})
    assert calls["n"] == 1  # no retry window burned

    # a load that CAN happen keeps the window: not manually off, model
    # installed -> second attempt occurs
    monkeypatch.setattr(lm, "server_manually_off", lambda: False)
    monkeypatch.setattr(lm, "installed_models", lambda: [{"id": "m"}])
    calls["n"] = 0
    with _pytest.raises(_rq.ConnectionError):
        client._post_with_retry("http://127.0.0.1:8199/v1/chat/completions",
                                 {}, {"stream": False})
    assert calls["n"] > 1
