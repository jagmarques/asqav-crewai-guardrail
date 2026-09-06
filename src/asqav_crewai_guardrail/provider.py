"""Evaluate local tool policy and request Asqav signing for its decision."""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from asqav.extras._base import AsqavAdapter

logger = logging.getLogger("asqav")

__all__ = [
    "GuardrailRequest",
    "GuardrailDecision",
    "GuardrailProvider",
    "PolicyFn",
    "AsqavGuardrailProvider",
]

# Action type recorded on the receipt. Distinguishes an authorization gate
# (pre-dispatch, allow/deny) from a start/end observation event.
_AUTHORIZE_ACTION_TYPE = "tool:authorize"

# Default policy verdict reason when no custom policy or denylist matches.
_DEFAULT_ALLOW_REASON = "default-allow"


@dataclass
class GuardrailRequest:
    """Context passed to the provider for each tool call.

    The adapter constructs this package-owned request from a CrewAI hook context.
    """

    tool_name: str
    tool_input: dict
    agent_role: str | None = None
    task_description: str | None = None
    crew_id: str | None = None
    timestamp: str = ""


@dataclass
class GuardrailDecision:
    """Provider allow/deny verdict with optional receipt metadata."""

    allow: bool
    reason: str | None = None
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class GuardrailProvider(Protocol):
    """Contract for pluggable tool-call authorization (crewAI #4877)."""

    name: str

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """Evaluate whether a tool call should proceed."""
        ...

    def health_check(self) -> bool:
        """Readiness probe. Default: True."""
        ...


# A user-supplied decision function. Returns (allow, reason). It owns the
# policy; asqav owns the receipt for whatever it decides.
PolicyFn = Callable[[GuardrailRequest], "tuple[bool, str]"]


class AsqavGuardrailProvider(AsqavAdapter):
    """Evaluate tool requests and attempt to sign the resulting decisions.

    A successful signing request adds its receipt identifier to the decision.
    Signing failure and observation mode can produce a decision without one.

    Args:
        policy: Optional callable ``(request) -> (allow, reason)``. Use this to
            supply a custom decision function. When omitted, the provider allows all
            calls (audit mode); combine with ``denied_tools`` for a simple
            blocklist.
        denied_tools: Optional set of tool names to always deny. Applied before
            ``policy``. Convenience for the common case.
        fail_closed: When True (default), an asqav API error denies the tool
            call. When False, the decision proceeds and the receipt is dropped.
        action_type: Receipt action type. Defaults to ``tool:authorize``.
        api_key / agent_name / agent_id / observe: Forwarded to the asqav
            adapter base (see ``asqav-crewai``).

    """

    name = "asqav"

    def __init__(
        self,
        *,
        policy: PolicyFn | None = None,
        denied_tools: "set[str] | list[str] | None" = None,
        fail_closed: bool = True,
        action_type: str = _AUTHORIZE_ACTION_TYPE,
        **adapter_kwargs: Any,
    ) -> None:
        super().__init__(**adapter_kwargs)
        self._policy = policy
        self._denied: set[str] = set(denied_tools or ())
        self._fail_closed = fail_closed
        self._action_type = action_type

    def _decide(self, request: GuardrailRequest) -> "tuple[bool, str]":
        """Run denylist then custom policy. Returns (allow, reason)."""
        if request.tool_name in self._denied:
            return False, f"denied by denylist: {request.tool_name}"
        if self._policy is not None:
            return self._policy(request)
        return True, _DEFAULT_ALLOW_REASON

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """Evaluate a tool request and attempt to record its verdict through Asqav."""
        allow, reason = self._decide(request)
        policy_decision = "permit" if allow else "deny"

        context = {
            "tool": request.tool_name,
            "agent_role": request.agent_role,
            "crew_id": request.crew_id,
            "task": request.task_description,
            "verdict": policy_decision,
        }

        receipt_id: str | None = None
        if not self._observe:
            try:
                sig = self._agent.sign(
                    self._action_type,
                    context,
                    tool_name=request.tool_name,
                    policy_decision=policy_decision,
                    reason=reason,
                )
                self._signatures.append(sig)
                receipt_id = getattr(sig, "signature_id", None)
            except Exception as exc:
                # asqav is the evidence layer. If it is unreachable we cannot
                # produce a receipt for this decision. Fail closed by default:
                # deny the call rather than act without a record.
                with suppress(Exception):
                    logger.warning("asqav authorize receipt failed: %s", exc)
                if self._fail_closed:
                    return GuardrailDecision(
                        allow=False,
                        reason="asqav signing failed",
                        metadata={"fail_closed": True, "policy_decision": policy_decision},
                    )
        else:
            with suppress(Exception):
                logger.info(
                    "OBSERVE: would sign %s verdict=%s tool=%s",
                    self._action_type,
                    policy_decision,
                    request.tool_name,
                )

        return GuardrailDecision(
            allow=allow,
            reason=reason,
            metadata={
                "receipt_id": receipt_id,
                "policy_decision": policy_decision,
            },
        )

    def health_check(self) -> bool:
        """Readiness probe.

        Confirms the asqav SDK is initialized and a provider agent is bound.
        Intentionally network-free so it is safe to call on every crew start.
        A live probe can be layered on by subclassing and overriding.
        """
        from asqav import client as _client

        return bool(getattr(_client, "_api_key", None)) and self._agent is not None
