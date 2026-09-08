import json

import pytest

import agents.llm_agents as llm_agents
from agents.state_refactored import AgentState


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)
        self.is_error = status_code >= 400

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, responses, requests):
        self._responses = responses
        self._requests = requests

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, json, headers):
        self._requests.append(dict(json))
        return self._responses.pop(0)


def _unsupported(param):
    return _FakeResponse(
        400,
        {
            "error": {
                "message": (
                    f"Unsupported value: '{param}' only supports its default value."
                ),
                "type": "invalid_request_error",
                "param": param,
                "code": "unsupported_value",
            }
        },
    )


def _success():
    return _FakeResponse(
        200,
        {
            "choices": [{"message": {"content": '{"status":"ok"}'}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        },
    )


def test_call_llm_uses_model_default_sampling_parameters(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []
    responses = [_success()]
    monkeypatch.setattr(
        llm_agents.httpx,
        "Client",
        lambda timeout: _FakeClient(responses, requests),
    )
    state = AgentState.create(goal="test", workspace=".")

    result = llm_agents.call_llm(
        "Return JSON.",
        "Test.",
        model="luna-test",
        retries=0,
        state=state,
        usage_scope="feedback",
    )

    assert result == {"status": "ok"}
    assert len(requests) == 1
    assert "temperature" not in requests[0]
    assert "top_p" not in requests[0]
    assert "llm_compatibility_fallbacks" not in state.context
    assert state.context["token_usage"]["calls"] == 1


def test_call_llm_omits_explicitly_unsupported_response_format(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []
    responses = [_unsupported("response_format"), _success()]
    monkeypatch.setattr(
        llm_agents.httpx,
        "Client",
        lambda timeout: _FakeClient(responses, requests),
    )
    state = AgentState.create(goal="test", workspace=".")

    result = llm_agents.call_llm(
        "Return JSON.",
        "Test.",
        model="compatible-test",
        retries=0,
        state=state,
    )

    assert result == {"status": "ok"}
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]
    assert state.context["llm_compatibility_fallbacks"][0][
        "omitted_parameter"
    ] == "response_format"


def test_call_llm_does_not_hide_unrelated_bad_requests(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []
    responses = [
        _FakeResponse(
            400,
            {
                "error": {
                    "message": "Invalid model identifier.",
                    "param": "model",
                    "code": "invalid_value",
                }
            },
        )
    ]
    monkeypatch.setattr(
        llm_agents.httpx,
        "Client",
        lambda timeout: _FakeClient(responses, requests),
    )

    with pytest.raises(RuntimeError, match="Invalid model identifier"):
        llm_agents.call_llm(
            "Return JSON.",
            "Test.",
            model="bad-model",
            retries=0,
        )

    assert len(requests) == 1
