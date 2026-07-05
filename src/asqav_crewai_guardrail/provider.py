"""crewAI GuardrailProvider backed by asqav.

Implements the GuardrailProvider protocol proposed in crewAI #4877
(evaluate + health_check). On every evaluate() the provider records the
authorization decision as an asqav receipt (the pre-dispatch authorization
receipt) and returns the verdict plus the receipt id in the decision metadata.

This is distinct from ``asqav-crewai``: that package signs tool:start / tool:end
events for audit (fail-open by default). This package is an authorization gate
that blocks or allows the call and produces a receipt for the verdict itself
(fail-closed by default).
"""

from __future__ import annotations

import logging
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

    Mirrors the contract proposed in crewAI #4877 so this package stays a
    drop-in once the protocol lands in crewAI core.
    """

    tool_name: str
    tool_input: dict
    agent_role: str | None = None
    task_description: str | None = None
    crew_id: str | None = None
    timestamp: str = ""


@dataclass
class GuardrailDecision:
    """Provider allow/deny verdict plus the asqav receipt for it."""

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
    """GuardrailProvider that records every authorization as an asqav receipt.

    The provider computes an allow/deny verdict (via a pluggable ``policy``
    callback, a ``denied_tools`` set, or a default allow-all) and then signs
    that verdict through asqav. The receipt travels with the action, so the
    question "who authorized this tool call and when?" is always answerable
    from a tamper-evident record, for both the allow path and the deny path.

    Args:
        policy: Optional callable ``(request) -> (allow, reason)``. Use this to
            slot in SINT, a YAML rules engine, a denylist with logic, or any
            custom decision function. When omitted, the provider allows all
            calls (audit mode); combine with ``denied_tools`` for a simple
            blocklist.
        denied_tools: Optional set of tool names to always deny. Applied before
            ``policy``. Convenience for the common case.
        fail_closed: When True (default), an asqav API error denies the tool
            call. When False, the decision proceeds and the receipt is dropped.
        action_type: Receipt action type. Defaults to ``tool:authorize``.
        api_key / agent_name / agent_id / observe: Forwarded to the asqav
            adapter base (see ``asqav-crewai``).

    The ``escalate`` tri-state from the #4877 discussion is expressed as
    ``allow=False`` with a reason of "awaiting approval". The eventual human
    approval is recorded as a second receipt countersigned onto this one
    (``Agent.countersign``), so the suspend/resolve lifecycle composes without
    new SDK surface.
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
        """Authorize a tool call and record the verdict as an asqav receipt."""
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
                logger.warning("asqav authorize receipt failed: %s", exc)
                if self._fail_closed:
                    return GuardrailDecision(
                        allow=False,
                        reason=f"asqav unreachable: {exc}",
                        metadata={"fail_closed": True, "policy_decision": policy_decision},
                    )
        else:
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
