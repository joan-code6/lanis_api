import asyncio
import copy
import json

import pytest
import requests

from api.ai_agent import (
    AIConfig,
    AIProviderError,
    AgentTool,
    OpenRouterClient,
    run_agent,
)


class _Response:
    def __init__(self, payload, status_code=200, *, body=None, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.is_redirect = 300 <= status_code < 400
        self.body = body if body is not None else json.dumps(payload).encode()
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


def _config():
    return AIConfig(
        "https://ai.example/v1/chat/completions", "development-key", "model"
    )


def test_ai_config_uses_exact_lowercase_environment_names(monkeypatch):
    monkeypatch.setenv("ai_endpoint", "https://ai.example/v1/chat/completions")
    monkeypatch.setenv("ai_api_key", "key")
    monkeypatch.setenv("ai_default_model", "openai/gpt-5.6-luna")

    config = AIConfig.from_env()

    assert config.configured is True
    assert config.default_model == "openai/gpt-5.6-luna"


def test_ai_config_rejects_insecure_or_base_endpoints():
    assert (
        AIConfig("http://ai.example/chat/completions", "key", "model").configured
        is False
    )
    assert AIConfig("https://ai.example/v1", "key", "model").configured is False


def test_agent_can_reason_call_tools_and_continue(monkeypatch):
    requests = []
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "hidden"}
                        ],
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "get_value",
                                    "arguments": '{"number":7}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {"message": {"role": "assistant", "content": "Der Wert ist 14."}}
            ]
        },
    ]

    def post(_url, **kwargs):
        requests.append(copy.deepcopy(kwargs))
        return _Response(responses.pop(0))

    monkeypatch.setattr("api.ai_agent.requests.post", post)
    calls = []

    async def double(arguments):
        calls.append(arguments)
        return {"success": True, "value": arguments["number"] * 2}

    result = asyncio.run(
        run_agent(
            config=_config(),
            system_prompt="system",
            user_message="double seven",
            tools=[
                AgentTool(
                    "get_value",
                    "Double a number",
                    {
                        "type": "object",
                        "properties": {"number": {"type": "integer"}},
                        "required": ["number"],
                        "additionalProperties": False,
                    },
                    double,
                )
            ],
        )
    )

    assert result == "Der Wert ist 14."
    assert calls == [{"number": 7}]
    second_messages = requests[1]["json"]["messages"]
    assert second_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"success":true,"value":14}',
    }
    assert second_messages[-2]["reasoning_details"][0]["text"] == "hidden"
    assert requests[0]["allow_redirects"] is False
    assert requests[0]["json"]["provider"]["zdr"] is True


def test_agent_does_not_execute_unknown_tools(monkeypatch):
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "bad",
                                "function": {
                                    "name": "delete_everything",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"role": "assistant", "content": "Das geht nicht."}}]},
    ]
    requests = []

    def post(_url, **kwargs):
        requests.append(copy.deepcopy(kwargs))
        return _Response(responses.pop(0))

    monkeypatch.setattr("api.ai_agent.requests.post", post)
    result = asyncio.run(
        run_agent(
            config=_config(), system_prompt="system", user_message="bad", tools=[]
        )
    )
    assert result == "Das geht nicht."
    tool_result = json.loads(requests[1]["json"]["messages"][-1]["content"])
    assert tool_result["error"] == "Unknown or unauthorized tool"


def test_agent_filters_tool_calls_without_ids(monkeypatch):
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {"function": {"name": "get_value", "arguments": "{}"}},
                            {
                                "id": "valid-call",
                                "function": {
                                    "name": "get_value",
                                    "arguments": "{}",
                                },
                            },
                        ],
                    }
                }
            ]
        },
        {"choices": [{"message": {"role": "assistant", "content": "Fertig."}}]},
    ]
    requests_sent = []

    def post(_url, **kwargs):
        requests_sent.append(copy.deepcopy(kwargs))
        return _Response(responses.pop(0))

    async def value(_arguments):
        return {"success": True}

    monkeypatch.setattr("api.ai_agent.requests.post", post)
    result = asyncio.run(
        run_agent(
            config=_config(),
            system_prompt="system",
            user_message="test",
            tools=[AgentTool("get_value", "Get value", {}, value)],
        )
    )

    assert result == "Fertig."
    continued = requests_sent[1]["json"]["messages"]
    assert [call["id"] for call in continued[-2]["tool_calls"]] == ["valid-call"]
    assert continued[-1]["tool_call_id"] == "valid-call"


def test_client_rejects_error_envelope_even_with_http_200(monkeypatch):
    monkeypatch.setattr(
        "api.ai_agent.requests.post",
        lambda *_args, **_kwargs: _Response(
            {"error": {"code": 429, "message": "secret upstream detail"}}
        ),
    )
    with pytest.raises(AIProviderError, match="error 429"):
        asyncio.run(OpenRouterClient(_config()).complete([], []))


def test_client_wraps_transport_errors(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.Timeout("upstream timed out")

    monkeypatch.setattr("api.ai_agent.requests.post", fail)

    with pytest.raises(AIProviderError, match="request failed"):
        asyncio.run(OpenRouterClient(_config()).complete([], []))


def test_client_rejects_oversized_provider_response(monkeypatch):
    response = _Response(
        {},
        body=b"x",
        headers={"Content-Length": str(2 * 1024 * 1024 + 1)},
    )
    monkeypatch.setattr(
        "api.ai_agent.requests.post", lambda *_args, **_kwargs: response
    )

    with pytest.raises(AIProviderError, match="too large"):
        asyncio.run(OpenRouterClient(_config()).complete([], []))

    assert response.closed is True


def test_client_rejects_oversized_stream_without_content_length(monkeypatch):
    response = _Response({}, body=b"x" * (2 * 1024 * 1024 + 1))
    monkeypatch.setattr(
        "api.ai_agent.requests.post", lambda *_args, **_kwargs: response
    )

    with pytest.raises(AIProviderError, match="too large"):
        asyncio.run(OpenRouterClient(_config()).complete([], []))

    assert response.closed is True
