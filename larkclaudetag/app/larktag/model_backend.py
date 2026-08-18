"""Where the agent's model comes from: a LiteLLM gateway, or Bedrock directly.

The Claude Agent SDK runs the `claude` CLI as a subprocess, so picking a backend
means handing that subprocess the right env vars. This module is the single place
that mapping lives; `main.py` and `tests/smoke_model_backend.py` both read it, so
a local smoke test exercises the same wiring the Runtime uses.

`MODEL_BACKEND` picks one (default `litellm`, the configuration this sample ships):

| MODEL_BACKEND | Transport                                       | Auth        | Model env var                        |
|---------------|-------------------------------------------------|-------------|--------------------------------------|
| `litellm`     | LiteLLM gateway, Anthropic /v1/messages shape   | gateway key | `LITELLM_MODEL` (gateway alias)      |
| `bedrock`     | Bedrock Invoke API                              | AWS SigV4   | `BEDROCK_MODEL` (inference profile)  |
| `mantle`      | Bedrock Mantle endpoint, native Anthropic shape | AWS SigV4   | `MANTLE_MODEL` (`anthropic.`-prefixed) |

Neither Bedrock backend needs an API key or a gateway: the CLI signs its requests
with the AWS credentials it inherits from this process, which on AgentCore Runtime
are the execution role's. That role therefore needs `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` (see docs/DEPLOY_RUNBOOK.md Step 5), and
the Runtime needs egress to the endpoint — either the public
`bedrock-runtime.<region>.amazonaws.com` or a VPC interface endpoint pointed at
via `ANTHROPIC_BEDROCK_BASE_URL`.

Why both Bedrock backends exist: the Invoke API is a pseudo-passthrough that
silently drops betas and built-in server-side tools, the same constraint LiteLLM
has, so `bedrock` inherits this sample's client-side-tools-only design. Mantle
serves the native Anthropic API shape, where that constraint does not apply. Its
model lineup is separate from the Bedrock catalog and access is granted per
account, which is why it is opt-in rather than the default.
"""

import os
from dataclasses import dataclass

DEFAULT_BACKEND = "litellm"

# A gateway alias, resolved by LiteLLM's config rather than by AWS.
DEFAULT_LITELLM_MODEL = "claude-opus-4-8"
# Cross-region inference profile id. The `global.` prefix lets Bedrock serve the
# request from whichever region has capacity instead of pinning one; a bare
# foundation-model id fails these models with "on-demand throughput isn't
# supported". Append `[1m]` to request the 1M-token context window.
DEFAULT_BEDROCK_MODEL = "global.anthropic.claude-opus-5"
# Mantle ids carry an `anthropic.` prefix, no version suffix and no region
# prefix — an inference profile id such as `global.anthropic.claude-opus-5`
# returns 400 here, because Mantle has its own model lineup.
DEFAULT_MANTLE_MODEL = "anthropic.claude-opus-5"

# Matches the Dockerfile's AWS_REGION, so a container that lost the var still
# resolves somewhere real instead of the CLI's own us-east-1 default.
FALLBACK_REGION = "us-west-2"


@dataclass(frozen=True)
class ModelBackend:
    """A resolved backend: which model to ask for, and the env the CLI needs."""

    name: str
    model: str
    env: dict[str, str]


def resolve() -> ModelBackend:
    """Read MODEL_BACKEND and build the CLI env for it.

    Raises ValueError on an unknown value: a typo'd backend name must fail the
    container at startup, because the alternative — quietly falling back to the
    default — would send every turn to a backend nobody chose.
    """
    name = os.environ.get("MODEL_BACKEND", DEFAULT_BACKEND).strip().lower() or DEFAULT_BACKEND
    builders = {"litellm": _litellm, "bedrock": _bedrock, "mantle": _mantle}
    if name not in builders:
        raise ValueError(
            f"MODEL_BACKEND={name!r} is not a known backend; use one of {sorted(builders)}"
        )
    return builders[name]()


def _litellm() -> ModelBackend:
    """LiteLLM gateway. The model is an alias the gateway maps to a provider."""
    return ModelBackend(
        name="litellm",
        model=os.environ.get("LITELLM_MODEL", DEFAULT_LITELLM_MODEL),
        # Set explicitly rather than left to inheritance: the CLI is a
        # subprocess, and being explicit here is what makes a missing gateway
        # var a visibly empty value instead of a silent fall-through to the
        # public Anthropic API.
        env={
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        },
    )


def _bedrock() -> ModelBackend:
    """Bedrock Invoke API, signed with this process's AWS credentials."""
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": _region(),
        # Only affects model ids the CLI resolves itself (an alias such as
        # `opus`), not the profile id pinned below — but without it a `us-*`
        # region would resolve those to `us.` profiles rather than `global.` ones.
        # Needs claude CLI >= 2.1.224; older versions ignore it.
        "ANTHROPIC_BEDROCK_REGION_PREFIX": os.environ.get(
            "ANTHROPIC_BEDROCK_REGION_PREFIX", "global"
        ),
        **_clear_gateway_vars(),
    }
    # A VPC interface endpoint for bedrock-runtime goes here, which is how a
    # VPC-mode Runtime reaches Bedrock without egressing to the public endpoint.
    _forward_if_set(env, "ANTHROPIC_BEDROCK_BASE_URL")
    return ModelBackend(
        name="bedrock",
        model=os.environ.get("BEDROCK_MODEL", DEFAULT_BEDROCK_MODEL),
        env=env,
    )


def _mantle() -> ModelBackend:
    """Bedrock Mantle endpoint: same AWS auth, native Anthropic API shape."""
    env = {
        "CLAUDE_CODE_USE_MANTLE": "1",
        "AWS_REGION": _region(),
        **_clear_gateway_vars(),
    }
    _forward_if_set(env, "ANTHROPIC_BEDROCK_MANTLE_BASE_URL")
    return ModelBackend(
        name="mantle",
        model=os.environ.get("MANTLE_MODEL", DEFAULT_MANTLE_MODEL),
        env=env,
    )


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or FALLBACK_REGION


def _clear_gateway_vars() -> dict[str, str]:
    """Blank the gateway vars on the Bedrock backends.

    The SDK merges `options.env` over the parent's `os.environ`, so it can set a
    var but never unset one. A runtime secret still carrying ANTHROPIC_BASE_URL
    from a LiteLLM deployment would otherwise point the CLI back at the gateway
    and keep serving the old backend with no error to show for it. Empty is as
    close to unset as the merge allows, and the CLI reads an empty value as absent.
    """
    return {"ANTHROPIC_BASE_URL": "", "ANTHROPIC_API_KEY": ""}


def _forward_if_set(env: dict[str, str], *names: str) -> None:
    """Forward optional vars only when set, so an unused knob stays absent in the
    subprocess instead of arriving as an empty override."""
    for name in names:
        value = os.environ.get(name)
        if value:
            env[name] = value
