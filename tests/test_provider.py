"""Tests for AsqavGuardrailProvider and the enable_guardrail adapter.

All tests run network-free: the asqav client is patched in conftest.py and the
crewai hook system is faked. Tests assert behavior, not just that code runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import asqav.client
import pytest

from asqav_crewai_guardrail import (
    AsqavGuardrailProvider,
    GuardrailDecision,
    GuardrailProvider,
    GuardrailRequest,
    enable_guardrail,
)


def _req(tool="search", **kw):
    base = {"tool_name": tool, "tool_input": {"q": "x"}}
    base.update(kw)
    return GuardrailRequest(**base)


# --- protocol conformance -------------------------------------------------


def test_provider_satisfies_protocol(fake_agent):
    provider = AsqavGuardrailProvider()
    # runtime_checkable Protocol: checks name + evaluate + health_check present
    assert isinstance(provider, GuardrailProvider)
    assert provider.name == "asqav"


# --- default allow + receipt ---------------------------------------------


def test_default_allow_produces_permit_receipt(fake_agent):
    provider = AsqavGuardrailProvider()
    decision = provider.evaluate(_req())

    assert decision.allow is True
    assert decision.reason == "default-allow"
    assert decision.metadata["receipt_id"] == "sig_test123"
    assert decision.metadata["policy_decision"] == "permit"

    fake_agent.sign.assert_called_once()
    _, kwargs = fake_agent.sign.call_args
    # action type + context are positional
    assert fake_agent.sign.call_args.args[0] == "tool:authorize"
    assert kwargs["tool_name"] == "search"
    assert kwargs["policy_decision"] == "permit"


# --- denylist records a deny receipt too ---------------------------------


def test_denylist_denies_and_still_records_receipt(fake_agent):
    provider = AsqavGuardrailProvider(denied_tools={"shell"})
    decision = provider.evaluate(_req(tool="shell"))

    assert decision.allow is False
    assert "denied by denylist" in decision.reason
    # the deny path MUST still produce a receipt (chain of evidence)
    assert decision.metadata["receipt_id"] == "sig_test123"
    assert decision.metadata["policy_decision"] == "deny"
    _, kwargs = fake_agent.sign.call_args
    assert kwargs["policy_decision"] == "deny"


# --- custom policy --------------------------------------------------------


def test_custom_policy_drives_verdict(fake_agent):
    def policy(req):
        return (False, "blocked: regulated tool")

    provider = AsqavGuardrailProvider(policy=policy)
    decision = provider.evaluate(_req(tool="wire_transfer"))

    assert decision.allow is False
    assert decision.reason == "blocked: regulated tool"
    assert decision.metadata["policy_decision"] == "deny"


def test_custom_policy_can_allow(fake_agent):
    provider = AsqavGuardrailProvider(policy=lambda r: (True, "on-allowlist"))
    decision = provider.evaluate(_req())
    assert decision.allow is True
    assert decision.reason == "on-allowlist"


# --- fail-closed vs fail-open on asqav error ------------------------------


def test_fail_closed_on_sign_error(fake_agent):
    fake_agent.sign.side_effect = RuntimeError("503 service unavailable")
    provider = AsqavGuardrailProvider(fail_closed=True)

    decision = provider.evaluate(_req())

    assert decision.allow is False
    assert decision.metadata["fail_closed"] is True
    # no receipt when the API failed
    assert "receipt_id" not in decision.metadata or decision.metadata.get("receipt_id") is None


def test_fail_open_on_sign_error(fake_agent):
    fake_agent.sign.side_effect = RuntimeError("timeout")
    provider = AsqavGuardrailProvider(fail_closed=False)

    decision = provider.evaluate(_req())

    # default policy is allow, so fail-open lets it through with no receipt
    assert decision.allow is True
    assert decision.metadata["receipt_id"] is None


# --- observe mode never signs --------------------------------------------


def test_observe_mode_does_not_sign(fake_agent):
    provider = AsqavGuardrailProvider(observe=True)
    decision = provider.evaluate(_req())

    assert decision.allow is True
    assert decision.metadata["receipt_id"] is None
    fake_agent.sign.assert_not_called()


# --- health_check ---------------------------------------------------------


def test_health_check_true_when_initialized(fake_agent):
    provider = AsqavGuardrailProvider()
    assert provider.health_check() is True


def test_health_check_false_without_api_key(fake_agent, monkeypatch):
    provider = AsqavGuardrailProvider()
    monkeypatch.setattr(asqav.client, "_api_key", None, raising=False)
    assert provider.health_check() is False


# --- receipt id flows from signature --------------------------------------


def test_receipt_id_matches_signature_id(fake_agent):
    from types import SimpleNamespace

    fake_agent.sign.return_value = SimpleNamespace(signature_id="sig_xyz789")
    provider = AsqavGuardrailProvider()
    decision = provider.evaluate(_req())
    assert decision.metadata["receipt_id"] == "sig_xyz789"


# --- enable_guardrail adapter wiring -------------------------------------


def test_enable_guardrail_blocks_denied_call(fake_agent, fake_crewai):
    provider = AsqavGuardrailProvider(denied_tools={"shell"})
    enable_guardrail(provider)

    hook = fake_crewai["hook"]
    assert hook is not None

    ctx = SimpleNamespace(
        tool_name="shell",
        tool_input={"cmd": "rm -rf /"},
        agent=SimpleNamespace(role="Operator"),
        task=SimpleNamespace(description="do work"),
        crew=None,
    )
    # denied -> hook returns False to block
    assert hook(ctx) is False


def test_enable_guardrail_allows_permitted_call(fake_agent, fake_crewai):
    provider = AsqavGuardrailProvider()
    enable_guardrail(provider)
    hook = fake_crewai["hook"]

    ctx = SimpleNamespace(
        tool_name="search",
        tool_input={"q": "cats"},
        agent=None,
        task=None,
        crew=None,
    )
    # allowed -> hook returns None (CrewAI treats non-False as allow)
    assert hook(ctx) is None


def test_enable_guardrail_hook_fail_closed_on_provider_raise(fake_agent, fake_crewai):
    class BoomProvider:
        name = "boom"

        def evaluate(self, request):
            raise RuntimeError("provider exploded")

        def health_check(self):
            return True

    enable_guardrail(BoomProvider(), fail_closed=True)
    hook = fake_crewai["hook"]

    assert (
        hook(SimpleNamespace(tool_name="x", tool_input={}, agent=None, task=None, crew=None))
        is False
    )


def test_enable_guardrail_hook_fail_open_on_provider_raise(fake_agent, fake_crewai):
    class BoomProvider:
        name = "boom"

        def evaluate(self, request):
            raise RuntimeError("provider exploded")

        def health_check(self):
            return True

    enable_guardrail(BoomProvider(), fail_closed=False)
    hook = fake_crewai["hook"]

    assert (
        hook(SimpleNamespace(tool_name="x", tool_input={}, agent=None, task=None, crew=None))
        is None
    )


# regression: crewAI fills tool_input from json.loads of the model args, so a
# non-dict (JSON scalar/array) can arrive. The old hook coerced it with dict()
# outside its fail-closed try, so the raise escaped and the tool ran unguarded.


@pytest.mark.parametrize("bad_input", [5, "x", "rm -rf /", [1, 2], 3.14, True])
def test_enable_guardrail_non_dict_tool_input_fails_closed(fake_agent, fake_crewai, bad_input):
    provider = AsqavGuardrailProvider(denied_tools={"shell"})
    enable_guardrail(provider, fail_closed=True)
    hook = fake_crewai["hook"]

    ctx = SimpleNamespace(
        tool_name="shell",
        tool_input=bad_input,
        agent=None,
        task=None,
        crew=None,
    )
    # must reach the guardrail and block, never raise out of the hook
    assert hook(ctx) is False


def test_enable_guardrail_non_dict_tool_input_reaches_provider_as_dict(fake_agent, fake_crewai):
    seen = {}

    class RecordingProvider:
        name = "rec"

        def evaluate(self, request):
            seen["tool_input"] = request.tool_input
            return GuardrailDecision(allow=False, reason="deny")

        def health_check(self):
            return True

    enable_guardrail(RecordingProvider(), fail_closed=True)
    hook = fake_crewai["hook"]

    ctx = SimpleNamespace(tool_name="wire", tool_input=[1, 2], agent=None, task=None, crew=None)
    assert hook(ctx) is False
    # the non-dict input reached the provider wrapped as a dict it can evaluate
    assert seen["tool_input"] == {"input": [1, 2]}


# regression: a provider whose evaluate() returns something that is not a
# GuardrailDecision (a forgotten return in a custom provider, the package's
# main extension point) must fail closed, not escape via decision.allow.


@pytest.mark.parametrize("bad_decision", [None, {"allow": False}])
def test_enable_guardrail_non_decision_return_fails_closed(fake_agent, fake_crewai, bad_decision):
    class MisbehavingProvider:
        name = "misbehaving"

        def evaluate(self, request):
            return bad_decision

        def health_check(self):
            return True

    enable_guardrail(MisbehavingProvider(), fail_closed=True)
    hook = fake_crewai["hook"]

    ctx = SimpleNamespace(tool_name="x", tool_input={}, agent=None, task=None, crew=None)
    assert hook(ctx) is False


def test_enable_guardrail_without_crewai_raises(fake_agent, monkeypatch):
    # remove any faked crewai so the import genuinely fails
    import sys

    monkeypatch.setitem(sys.modules, "crewai", None)
    monkeypatch.setitem(sys.modules, "crewai.hooks", None)
    provider = AsqavGuardrailProvider()
    with pytest.raises(ImportError, match="crewai"):
        enable_guardrail(provider)


# --- GuardrailDecision shape ---------------------------------------------


def test_decision_dataclass_defaults():
    d = GuardrailDecision(allow=True)
    assert d.reason is None
    assert d.metadata == {}
