# Asqav GuardrailProvider for CrewAI

This package evaluates tool requests with a local policy and attempts to record each decision through [Asqav](https://asqav.com). Its CrewAI before-tool hook returns `False` when the provider denies the request. A successful signing request adds a receipt identifier to the decision metadata.

The package is maintained by Asqav and requires an Asqav API key for signing. It does not independently verify receipts or prove that an allowed tool executed.

## Install and configure

Install the source with its CrewAI extra:

```bash
python -m pip install "asqav-crewai-guardrail[crewai] @ git+https://github.com/jagmarques/asqav-crewai-guardrail.git"
```

The supported dependency range is CrewAI 1.9.1 through the 1.x series and Asqav SDK 0.10.10 through the 0.10 series. Runtime checks exercise CrewAI 1.9.1 and 1.9.3 with SDK 0.10.10.

Call `enable_guardrail` once per process during serial application setup, before registering other before-tool hooks or starting execution. Existing hooks and repeated calls cause a setup error; the helper leaves the registry unchanged. If you also use `asqav-crewai` observational hooks, enable this guard first.

```python
import asqav
from asqav_crewai_guardrail import AsqavGuardrailProvider, enable_guardrail

asqav.init()  # Reads ASQAV_API_KEY from the environment.
provider = AsqavGuardrailProvider(
    agent_name="my-crew",
    denied_tools={"shell", "file_write"},
)
enable_guardrail(provider)
# Configure the rest of your hooks and crew, then call Crew.kickoff().
```

Keep the guard registered. Concurrent setup or calls to CrewAI's clear/unregister APIs can invalidate its position. In the tested synchronous native and ReAct dispatchers, a denial stops execution before later hooks run. A later hook can still block an allowed call.

The policy sees input at this first callback. Later hooks must not change authorization-relevant inputs if callers rely on that decision. This integration does not establish coverage for every cached, skipped, asynchronous or custom framework path.

## Choose the policy

The denylist takes precedence over a custom policy. With neither a matching denylist entry nor a custom policy, the provider allows the request.

Use a policy callback instead of the provider configuration above:

```python
from asqav_crewai_guardrail import AsqavGuardrailProvider

def policy(request):
    if request.tool_name == "wire_transfer":
        return False, "approval required"
    return True, "allowed by local policy"

provider = AsqavGuardrailProvider(agent_name="my-crew", policy=policy)
```

A callback receives a `GuardrailRequest` and returns `(allow, reason)`. The request includes the tool name/input, agent role, task description and crew identifier when available. The provider records `permit` or `deny` with the reason. An approval workflow must be implemented by the application; this package supplies no suspension, approval or countersigning lifecycle.

## Failures and receipts

The provider and hook have separate `fail_closed` options, both defaulting to `True`:

| Condition | Default behavior | With the relevant option set to `False` |
| --- | --- | --- |
| Signing request fails | Provider denies; no receipt identifier | Provider retains the local policy decision without a receipt |
| Provider evaluation raises or returns the wrong type | Hook denies | Hook permits the request to continue |

Diagnostic logging cannot change these decisions. The SDK may retry retryable HTTP failures before returning an error. The `observe=True` provider option evaluates policy without requesting a signature; a deny decision still blocks a request that reaches the hook.

On successful signing, `GuardrailDecision.metadata["receipt_id"]` contains the returned signature identifier. Failed signing and observation mode do not guarantee a receipt. A decision receipt records authorization, not tool completion. `health_check()` inspects local SDK/provider state and makes no network request.

## Data sent to Asqav

The built-in provider constructs signing context from the tool name, agent role, crew identifier, task description and verdict. It also supplies the tool name, policy decision and reason to the SDK. It does not put raw tool input into that signing context; a custom policy receives the input locally.

SDK mode determines whether this context is hashed or sent in full. Identifiers and decision metadata can still be sent in hash-only mode. Set `ASQAV_MODE=hash-only` in the application environment when that mode is required; set `ASQAV_MODE=full-payload` to send the selected context. Other CrewAI components and model providers handle their own traffic separately.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

The suite includes real `Crew.kickoff()` native/ReAct dispatch with the actual SDK, plus focused unit tests. Model responses and HTTP replies are local fixtures; tests prohibit network connections. The framework cases check setup order, denial/allow execution, provider failures and throwing diagnostic handlers. They do not claim live service responses or cryptographic receipt verification.

The protocol dataclasses can be imported without CrewAI. `enable_guardrail()` requires CrewAI and its public hook API.

## License

[Elastic License 2.0](LICENSE).
