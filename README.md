<p align="center">
  <a href="https://asqav.com">
    <img src="https://asqav.com/logo-text-white.png" alt="Asqav" width="200">
  </a>
</p>
<p align="center">
  A crewAI GuardrailProvider that turns every tool-call decision into a signed receipt.
</p>
<p align="center">
  <a href="https://www.asqav.com/">Website</a> |
  <a href="https://www.asqav.com/docs">Docs</a> |
  <a href="https://github.com/jagmarques/asqav-sdk">SDK</a> |
  <a href="https://github.com/crewAIInc/crewAI/issues/4877">crewAI #4877</a>
</p>

# Asqav GuardrailProvider for crewAI

This package implements the `GuardrailProvider` protocol proposed in
[crewAI #4877](https://github.com/crewAIInc/crewAI/issues/4877). Every
authorization decision (allow or deny) is recorded through
[Asqav](https://asqav.com) as a cryptographic receipt, and the receipt id is
returned with the verdict.

It plugs into crewAI's `before_tool_call` hook. When the provider denies a call,
the hook returns `False` and crewAI blocks the tool. When it allows, the call
proceeds and both outcomes carry a tamper-evident record of who authorized
what, and when.

## How this differs from `asqav-crewai`

`asqav-crewai` signs `tool:start` and `tool:end` events for audit. It observes
and records, and fails open by default so governance never breaks your crew.

This package is an **authorization gate**. It decides whether a tool call may
run, fails closed by default (a refused or unreachable Asqav blocks the call),
and produces a receipt for the decision itself, not just the event. Use the two
together: this provider gates the call, `asqav-crewai` records its start and
end.

## Install

Not yet on PyPI. Install from GitHub:

```bash
pip install "asqav-crewai-guardrail[crewai] @ git+https://github.com/jagmarques/asqav-crewai-guardrail.git"
```

This pulls in the `asqav` SDK. crewAI is a peer dependency you install via the
`crewai` extra (shown above). Tool call hooks require crewAI 1.9.1 or newer.

## Usage

```python
import asqav
from crewai import Agent, Crew, Task
from crewai.tools import tool

from asqav_crewai_guardrail import AsqavGuardrailProvider, enable_guardrail

asqav.init(api_key="sk_...")

# Deny a small set of tools by name; allow everything else. Every decision,
# allow or deny, is recorded as an Asqav receipt.
provider = AsqavGuardrailProvider(
    agent_name="my-crew",
    denied_tools={"shell", "file_write"},
)
enable_guardrail(provider)


@tool("echo_tool")
def echo_tool(text: str) -> str:
    """Echo the given text back."""
    return f"echoed {text}"


agent = Agent(
    role="Echoer",
    goal="Echo one message via the echo tool",
    backstory="A minimal test agent.",
    tools=[echo_tool],
)
task = Task(
    description="Echo the message 'hello world' using echo_tool.",
    expected_output="The echoed message.",
    agent=agent,
)
result = Crew(agents=[agent], tasks=[task]).kickoff()
```

Each tool call now flows through Asqav before it runs. The receipt is signed
server-side with NIST FIPS 204 ML-DSA, so the audit trail is tamper-evident and
holds up for EU AI Act, DORA, and SOC 2 evidence.

Asqav governs the agents you wire through it. An agent that never routes
through the governed path produces no receipt and is not detected.

## Bring your own decision

Asqav is the evidence layer. The decision can come from anywhere. Pass a
`policy` callback and Asqav signs whatever it returns:

```python
def my_policy(request):
    # slot in SINT, a YAML rules engine, a risk scorer, or anything else
    if request.tool_name == "wire_transfer":
        return (False, "blocked: regulated tool")
    return (True, "on-allowlist")

provider = AsqavGuardrailProvider(agent_name="my-crew", policy=my_policy)
enable_guardrail(provider)
```

The callback receives a `GuardrailRequest` and returns a `(allow, reason)`
tuple. The provider records the verdict as `policy_decision="permit"` or
`"deny"` on the receipt, so you get a chain of evidence no matter which engine
made the call.

## Fail-closed by default

If the Asqav API is unreachable, the provider cannot produce a receipt for the
decision. By default it denies the call rather than act without a record:

```python
AsqavGuardrailProvider(fail_closed=True)   # default: block on Asqav error
AsqavGuardrailProvider(fail_closed=False)  # proceed and drop the receipt
```

The hook adapter has its own `fail_closed` flag that governs what happens if
`provider.evaluate` itself raises unexpectedly:

```python
enable_guardrail(provider, fail_closed=True)  # block on provider error
```

## The tri-state question (allow / deny / escalate)

The crewAI #4877 discussion converged on a tri-state verdict rather than a
boolean. The protocol dataclass shipped here is the bi-state `allow: bool` from
the proposal, so this package is a drop-in once the protocol lands in crewAI
core. The `escalate` state is expressed as `allow=False` with a reason of
"awaiting approval". A subsequent human approval is recorded as a second
receipt countersigned onto the original (see `Agent.countersign` in the SDK), so
the human-in-the-loop lifecycle composes without new SDK surface.

## How it works

`AsqavGuardrailProvider` extends the Asqav adapter base class. On each
`evaluate()`:

1. It runs the denylist, then the optional `policy` callback, then a default
   allow-all, producing an `(allow, reason)` verdict.
2. It calls `Agent.sign(action_type="tool:authorize", ...)` with
   `policy_decision="permit"` or `"deny"`, the tool name, agent role, and the
   verdict reason.
3. It returns a `GuardrailDecision` whose `metadata["receipt_id"]` is the Asqav
   signature id for that decision.

`enable_guardrail(provider)` registers a `before_tool_call` hook that maps the
crewAI `ToolCallHookContext` into a `GuardrailRequest`, calls the provider, and
returns `False` to block when the decision denies.

## Data handling

`asqav-crewai-guardrail` is a thin wrapper around the `asqav` Python SDK and
inherits its mode behavior:

- Asqav cloud on `*.asqav.com`: the SDK hashes your action context locally and
  sends only the hash plus a small metadata bag. Raw prompts and tool arguments
  never leave your infrastructure.
- Self-hosted: the SDK sends the full context so the server can run policy
  checks, PII redaction, and richer audit views.

## Configuration

```python
# Use an existing Asqav agent by ID
AsqavGuardrailProvider(agent_id="ag_abc123")

# Override the API key
AsqavGuardrailProvider(api_key="sk_other", agent_name="authz-crew")

# Observe only (no receipts written, decisions still returned)
AsqavGuardrailProvider(observe=True)
```

## License

Elastic License 2.0 (ELv2). See [LICENSE](LICENSE).
