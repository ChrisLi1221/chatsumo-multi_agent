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


@pytest.mark.parametrize('content', ['', '{"mutation_id":"mutation_001"}'])
def test_token_limit_is_not_json_error_and_is_not_silently_retried(monkeypatch, content):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    requests = []
    responses = [_FakeResponse(200, {
        'id': 'diagnostic-response', 'model': 'test-model',
        'choices': [{'finish_reason': 'length', 'message': {'content': content}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 500,
                  'completion_tokens_details': {'reasoning_tokens': 500}},
    })]
    monkeypatch.setattr(llm_agents.httpx, 'Client', lambda timeout: _FakeClient(responses, requests))
    state = AgentState.create(goal='test', workspace='.')
    with pytest.raises(llm_agents.LLMOutputError) as exc:
        llm_agents.call_llm('JSON', 'test', state=state, max_tokens=500, retries=2)
    assert exc.value.category == 'output_token_limit'
    assert len(requests) == 1
    log = state.context['llm_response_metadata'][0]
    assert log['finish_reason'] == 'length'
    assert log['raw_content'] == content
    assert log['max_completion_tokens'] == 500
    assert log['usage']['completion_tokens_details']['reasoning_tokens'] == 500
    assert state.context['token_usage']['completion_tokens'] == 500


@pytest.mark.parametrize(('choice', 'category'), [
    ({'finish_reason': 'stop', 'message': {'content': ''}}, 'empty_content'),
    ({'finish_reason': 'stop', 'message': {'content': 'not json'}}, 'invalid_json_object'),
    ({'finish_reason': 'stop', 'message': {'content': '[]'}}, 'invalid_json_object'),
    ({'finish_reason': 'stop', 'message': {'content': None, 'refusal': 'Declined'}}, 'refusal_or_filter'),
    ({'finish_reason': 'content_filter', 'message': {'content': ''}}, 'refusal_or_filter'),
    ({'finish_reason': 'tool_calls', 'message': {'content': ''}}, 'unexpected_tool_call'),
    (None, 'missing_choices'),
])
def test_distinguishes_failure_modes(monkeypatch, choice, category):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    responses = [_FakeResponse(200, {'choices': [choice] if choice else []})]
    monkeypatch.setattr(llm_agents.httpx, 'Client', lambda timeout: _FakeClient(responses, []))
    state = AgentState.create(goal='test', workspace='.')
    with pytest.raises(llm_agents.LLMOutputError) as exc:
        llm_agents.call_llm('JSON', 'test', state=state, retries=0)
    assert exc.value.category == category
    assert state.context['llm_response_metadata'][0]['outcome'] == category


def test_api_reported_output_limit_preserves_http_evidence(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    requests = []
    responses = [_FakeResponse(400, {'error': {'message':
        'Could not finish the message because max_tokens or model output limit was reached.'}})]
    monkeypatch.setattr(llm_agents.httpx, 'Client', lambda timeout: _FakeClient(responses, requests))
    state = AgentState.create(goal='test', workspace='.')
    with pytest.raises(llm_agents.LLMOutputError) as exc:
        llm_agents.call_llm('JSON', 'test', state=state, retries=2)
    assert exc.value.category == 'http_output_limit'
    assert len(requests) == 1
    assert state.context['llm_response_metadata'][0]['http_status'] == 400


def test_diagnostic_log_redacts_secret_in_response(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-secret-value')
    responses = [_FakeResponse(200, {'choices': [{'finish_reason': 'stop',
                  'message': {'content': '{"echo":"test-secret-value"}'}}]})]
    monkeypatch.setattr(llm_agents.httpx, 'Client', lambda timeout: _FakeClient(responses, []))
    state = AgentState.create(goal='test', workspace='.')
    llm_agents.call_llm('JSON', 'test', state=state, retries=0)
    assert 'test-secret-value' not in json.dumps(state.context)
    assert '[REDACTED]' in state.context['llm_response_metadata'][0]['raw_content']
