"""Actual CrewAI dispatch and SDK serialization, with local model/HTTP fixtures."""

import json
import logging
import socket

import asqav
import httpx
import pytest
from crewai import Agent, Crew, Task
from crewai.hooks import (
    clear_all_tool_call_hooks,
    get_before_tool_call_hooks,
    register_before_tool_call_hook,
)
from crewai.llms.base_llm import BaseLLM
from crewai.tools import tool

from asqav_crewai_guardrail import AsqavGuardrailProvider, enable_guardrail


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    import asqav.client as sdk

    monkeypatch.setenv("CREWAI_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ASQAV_MODE", raising=False)
    monkeypatch.setattr(sdk, "_api_base", "https://api.asqav.com/api/v1")
    clear_all_tool_call_hooks()
    state = {"tool": 0, "model": 0, "later": 0, "sign": [], "sign_error": False}

    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden in framework test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)

    def send(client, request, **kwargs):
        assert request.url.host == "api.asqav.com"
        body = json.loads(request.content)
        if request.url.path == "/api/v1/agents/create":
            data = {
                "agent_id": "agent_test",
                "name": body["name"],
                "public_key": "test_key",
                "key_id": "test_kid",
                "algorithm": "ml-dsa-65",
                "capabilities": [],
                "created_at": 0,
            }
        else:
            assert request.url.path == "/api/v1/agents/agent_test/sign"
            state["sign"].append(body)
            if state["sign_error"] == "unprintable":

                class UnprintableError(Exception):
                    def __str__(self):
                        raise RuntimeError("diagnostic string conversion failed")

                raise UnprintableError()
            if state["sign_error"]:
                return httpx.Response(403, json={"detail": "local refusal"}, request=request)
            data = {
                "signature": "response_fixture",
                "signature_id": "sig_test",
                "action_id": "action_test",
                "timestamp": 0,
                "verification_url": "https://example.invalid/test",
            }
        return httpx.Response(200, json=data, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    asqav.init(api_key="test-only-key")
    yield state
    clear_all_tool_call_hooks()
    if sdk._client is not None:
        sdk._client.close()


def execute(native, state, allowed):
    class LocalLLM(BaseLLM):
        def __init__(self):
            super().__init__(model="test/local")

        def supports_function_calling(self):
            return native

        def call(self, messages, **kwargs):
            state["model"] += 1
            assert state["model"] <= 2
            if state["model"] == 1:
                if native:
                    return [
                        {
                            "id": "echo",
                            "type": "function",
                            "function": {
                                "name": "echo_tool",
                                "arguments": '{"text":"hello"}',
                            },
                        }
                    ]
                return 'Thought: Echo.\nAction: echo_tool\nAction Input: {"text":"hello"}'
            expected = "echoed hello" if allowed else "Tool execution blocked by hook"
            assert expected in str(messages)
            return "Final Answer: checked"

    @tool("echo_tool")
    def echo_tool(text: str) -> str:
        """Echo a message and count actual execution."""
        state["tool"] += 1
        return f"echoed {text}"

    agent = Agent(
        role="Echoer",
        goal="Echo one message",
        backstory="Local fixture",
        tools=[echo_tool],
        llm=LocalLLM(),
        max_iter=3,
    )
    task = Task(description="Echo hello with echo_tool", expected_output="Echo", agent=agent)
    Crew(agents=[agent], tasks=[task], cache=False, tracing=False).kickoff()
    assert state["tool"] == int(allowed)
    assert state["model"] == 2


def break_diagnostics(monkeypatch, prefix):
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith(prefix):
                raise RuntimeError("application diagnostic handler failed")

    logger = logging.getLogger("asqav")
    monkeypatch.setattr(logger, "handlers", [BrokenHandler()])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)
    logger._cache.clear()


def test_setup_rejects_existing_and_repeated_hooks_without_mutation(runtime):
    provider = AsqavGuardrailProvider()

    def callback(context):
        return None

    register_before_tool_call_hook(callback)
    with pytest.raises(RuntimeError, match="before other before-tool hooks"):
        enable_guardrail(provider)
    assert get_before_tool_call_hooks() == [callback]
    clear_all_tool_call_hooks()
    assert enable_guardrail(provider) is provider
    registered = get_before_tool_call_hooks()
    with pytest.raises(RuntimeError, match="once"):
        enable_guardrail(provider)
    assert get_before_tool_call_hooks() == registered


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("allow", [True, False])
def test_first_guard_decision_precedes_later_hook_failure(runtime, native, allow):
    provider = AsqavGuardrailProvider(denied_tools=set() if allow else {"echo_tool"})
    enable_guardrail(provider)

    def later(context):
        runtime["later"] += 1
        raise RuntimeError("later callback failed")

    register_before_tool_call_hook(later)
    execute(native, runtime, allow)
    assert runtime["later"] == int(allow)
    assert len(runtime["sign"]) == 1
    assert runtime["sign"][0]["action_type"] == "tool:authorize"
    assert runtime["sign"][0]["policy_decision"] == ("permit" if allow else "deny")


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("hook_closed", [True, False])
@pytest.mark.parametrize(
    "path", ["deny", "provider_error", "sign_error", "observe", "unprintable_sign_error"]
)
def test_diagnostic_failures_preserve_decisions(runtime, monkeypatch, native, hook_closed, path):
    provider = AsqavGuardrailProvider(denied_tools={"echo_tool"}, observe=path == "observe")
    prefix = "guardrail "
    if path == "provider_error":

        def unavailable(request):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(provider, "evaluate", unavailable)
    elif path == "sign_error":
        runtime["sign_error"] = True
        prefix = "asqav authorize receipt failed:"
    elif path == "observe":
        prefix = "OBSERVE: would sign"
    elif path == "unprintable_sign_error":
        runtime["sign_error"] = "unprintable"
        prefix = "asqav authorize receipt failed:"
    break_diagnostics(monkeypatch, prefix)
    enable_guardrail(provider, fail_closed=hook_closed)
    execute(native, runtime, path == "provider_error" and not hook_closed)
    assert len(runtime["sign"]) == int(path in {"deny", "sign_error", "unprintable_sign_error"})


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("allow", [True, False])
def test_provider_fail_open_retains_the_policy_decision(runtime, monkeypatch, native, allow):
    provider = AsqavGuardrailProvider(
        denied_tools=set() if allow else {"echo_tool"}, fail_closed=False
    )
    runtime["sign_error"] = True
    break_diagnostics(monkeypatch, "asqav authorize receipt failed:")
    enable_guardrail(provider, fail_closed=False)
    execute(native, runtime, allow)


@pytest.mark.parametrize("native", [True, False])
def test_diagnostic_name_access_cannot_override_denial(runtime, native):
    from asqav_crewai_guardrail import GuardrailDecision

    class Provider:
        @property
        def name(self):
            raise RuntimeError("name unavailable")

        def evaluate(self, request):
            return GuardrailDecision(allow=False)

    enable_guardrail(Provider(), fail_closed=False)
    execute(native, runtime, False)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("hook_closed", [True, False])
def test_failed_verdict_truth_check_obeys_hook_setting(runtime, native, hook_closed):
    from asqav_crewai_guardrail import GuardrailDecision

    class BrokenTruth:
        def __bool__(self):
            raise RuntimeError("verdict evaluation failed")

    class Provider:
        name = "test"

        def evaluate(self, request):
            return GuardrailDecision(allow=BrokenTruth())

    enable_guardrail(Provider(), fail_closed=hook_closed)
    execute(native, runtime, not hook_closed)
