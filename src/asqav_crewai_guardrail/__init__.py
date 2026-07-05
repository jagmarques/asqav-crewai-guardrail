"""crewAI GuardrailProvider backed by asqav signed receipts.

See the crewAI #4877 proposal for the GuardrailProvider contract this package
implements.
"""

from .adapter import enable_guardrail
from .provider import (
    AsqavGuardrailProvider,
    GuardrailDecision,
    GuardrailProvider,
    GuardrailRequest,
    PolicyFn,
)

__all__ = [
    "AsqavGuardrailProvider",
    "GuardrailDecision",
    "GuardrailProvider",
    "GuardrailRequest",
    "PolicyFn",
    "enable_guardrail",
]

__version__ = "0.1.0"
