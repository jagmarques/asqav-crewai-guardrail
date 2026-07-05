"""crewAI hook adapter that wires a GuardrailProvider into CrewAI.

Implements the ``enable_guardrail(provider, *, fail_closed=True)`` helper from
the crewAI #4877 proposal. It registers a ``before_tool_call`` hook that
translates the crewAI ``ToolCallHookContext`` into a ``GuardrailRequest``,
calls the provider, and returns ``False`` to block the call when the provider
denies.

crewAI is imported lazily so the protocol dataclasses in this package are
usable without crewAI installed. ``enable_guardrail`` raises a clear error if
crewAI is missing.
"""

from __future__ import annotations

import logging

from .provider import GuardrailProvider, GuardrailRequest

logger = logging.getLogger("asqav")

__all__ = ["enable_guardrail"]


def enable_guardrail(
    provider: GuardrailProvider,
    *,
    fail_closed: bool = True,
) -> GuardrailProvider:
    """Wire a GuardrailProvider into CrewAI's before_tool_call hook system.

    The provider's own ``fail_closed`` setting governs asqav-API errors.
    This helper's ``fail_closed`` is a hook-level safety net: if
    ``provider.evaluate`` itself raises, the call is denied when
    ``fail_closed`` (the default) and allowed otherwise.

    Args:
        provider: Any object implementing the GuardrailProvider protocol.
        fail_closed: Hook-level fail-closed on an unexpected provider error.

    Returns:
        The same provider, for chaining.
    """
    try:
        from crewai.hooks import register_before_tool_call_hook
    except ImportError as err:
        raise ImportError(
            "enable_guardrail requires crewai. "
            "Install with: pip install 'asqav-crewai-guardrail[crewai]'"
        ) from err

    def _hook(context) -> bool | None:  # type: ignore[no-untyped-def]
        request = GuardrailRequest(
            tool_name=getattr(context, "tool_name", "") or "",
            tool_input=dict(getattr(context, "tool_input", None) or {}),
            agent_role=getattr(getattr(context, "agent", None), "role", None),
            task_description=getattr(getattr(context, "task", None), "description", None),
            crew_id=_crew_id(getattr(context, "crew", None)),
        )
        try:
            decision = provider.evaluate(request)
        except Exception:
            logger.warning(
                "guardrail provider %s raised; %s",
                getattr(provider, "name", "?"),
                "blocking (hook fail-closed)" if fail_closed else "allowing (hook fail-open)",
            )
            if fail_closed:
                return False
            return None
        if not decision.allow:
            logger.info(
                "guardrail %s blocked tool %s: %s",
                getattr(provider, "name", "?"),
                request.tool_name,
                decision.reason,
            )
            return False
        return None

    register_before_tool_call_hook(_hook)
    return provider


def _crew_id(crew: object | None) -> str | None:
    """Best-effort crew id across crewAI versions."""
    if crew is None:
        return None
    for attr in ("id", "key", "name"):
        value = getattr(crew, attr, None)
        if value:
            return str(value)
    return None
