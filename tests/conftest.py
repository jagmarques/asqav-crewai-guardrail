"""Shared test fixtures.

Patches the asqav client so provider tests run without a network and without a
real API key. Each test gets a fresh fake agent whose ``sign`` is a mock.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["OTEL_SDK_DISABLED"] = "true"

import asqav.client
import pytest


class FakeSignature:
    """Stand-in for asqav SignatureResponse with the field the provider reads."""

    def __init__(self, signature_id: str = "sig_test123") -> None:
        self.signature_id = signature_id


@pytest.fixture
def fake_agent(monkeypatch):
    """Initialize the asqav client with a fake key and a mock agent.

    Returns the mock agent so tests can configure/inspect ``.sign``.
    """
    monkeypatch.setattr(asqav.client, "_api_key", "sk_test_client", raising=False)

    agent = MagicMock()
    agent.agent_id = "ag_test"
    agent.name = "asqav-guardrail"
    agent.sign = MagicMock(return_value=FakeSignature("sig_test123"))
    monkeypatch.setattr(asqav.client.Agent, "create", lambda *a, **kw: agent)
    return agent


@pytest.fixture
def fake_crewai(monkeypatch):
    """Install a fake ``crewai.hooks`` module capturing the registered hook.

    Yields the hook-recorder so adapter tests can invoke the hook directly.
    Lets enable_guardrail run without crewai installed.
    """
    recorder = {"hook": None}

    def fake_register(hook):
        recorder["hook"] = hook

    hooks_mod = types.ModuleType("crewai.hooks")
    hooks_mod.register_before_tool_call_hook = fake_register
    hooks_mod.get_before_tool_call_hooks = lambda: [recorder["hook"]] if recorder["hook"] else []
    crewai_mod = types.ModuleType("crewai")
    crewai_mod.hooks = hooks_mod

    monkeypatch.setitem(sys.modules, "crewai", crewai_mod)
    monkeypatch.setitem(sys.modules, "crewai.hooks", hooks_mod)
    return recorder
