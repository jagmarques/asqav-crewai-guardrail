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
from contextlib import suppress

from .provider import GuardrailDecision, GuardrailProvider, GuardrailRequest

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

    Call once during serial setup, before other before-tool hooks. Existing
    hooks cause a RuntimeError and remain unchanged. Keep the guard registered;
    later hooks must preserve authorization-relevant inputs.

    Returns:
        The same provider, for chaining.
    """
    try:
        from crewai.hooks import get_before_tool_call_hooks, register_before_tool_call_hook
    except ImportError as err:
        raise ImportError(
            "enable_guardrail requires crewai. Install with: pip install 'crewai>=1.9.1,<2'"
        ) from err

    if get_before_tool_call_hooks():
        raise RuntimeError(
            "enable_guardrail must run once, before other before-tool hooks, "
            "during serial application setup"
        )

    def _hook(context) -> bool | None:  # type: ignore[no-untyped-def]
        # Build and evaluate inside the try so ANY error here fails closed
        # instead of escaping into crewAI's exception-swallowing dispatch.
        try:
            request = GuardrailRequest(
                tool_name=getattr(context, "tool_name", "") or "",
                tool_input=_coerce_tool_input(getattr(context, "tool_input", None)),
                agent_role=getattr(getattr(context, "agent", None), "role", None),
                task_description=getattr(getattr(context, "task", None), "description", None),
                crew_id=_crew_id(getattr(context, "crew", None)),
            )
            decision = provider.evaluate(request)
            if not isinstance(decision, GuardrailDecision):
                # A custom provider (the package's main extension point) that
                # forgets a return, or returns a raw dict, must not read as an
                # authorized allow. Raise so the fail-closed except below denies.
                raise TypeError(
                    f"{getattr(provider, 'name', '?')}.evaluate() returned "
                    f"{type(decision).__name__}, expected GuardrailDecision"
                )
            allowed = bool(decision.allow)
        except Exception:
            # No signed receipt on this path: asqav is unreachable or the
            # provider is broken, so there is nothing to sign. Still deny.
            with suppress(Exception):
                logger.warning(
                    "guardrail %s could not authorize the call; %s",
                    getattr(provider, "name", "?"),
                    "blocking (hook fail-closed)" if fail_closed else "allowing (hook fail-open)",
                )
            if fail_closed:
                return False
            return None
        if not allowed:
            with suppress(Exception):
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


def _coerce_tool_input(raw: object) -> dict:
    """Return a dict the guardrail can evaluate, never raising.

    crewAI fills tool_input from json.loads of the model args, so a JSON scalar
    or array can arrive. A dict passes through, None becomes empty, and anything
    else is wrapped as ``{"input": value}`` so the provider sees it.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None:
        return {}
    return {"input": raw}


def _crew_id(crew: object | None) -> str | None:
    """Best-effort crew id across crewAI versions."""
    if crew is None:
        return None
    for attr in ("id", "key", "name"):
        value = getattr(crew, attr, None)
        if value:
            return str(value)
    return None
