"""Smoke test: Claude Agent SDK -> the selected model backend, with a dummy tool.

Confirms the riskiest assumption on any backend — that client-side function
calling survives it, i.e. the tool is offered and Claude can call it. If this
passes, the agent's MCP/@tool surface will work. It matters most on the two
pseudo-passthrough paths (`litellm` and `bedrock`), which silently drop betas and
built-in server tools; a dropped *client* tool would break the whole agent.

Backend selection and env wiring come from the agent's own model_backend module,
so this exercises the same mapping main.py uses rather than a copy of it.

Run locally (needs the system `claude` CLI):

  # LiteLLM gateway (default)
  export ANTHROPIC_BASE_URL=http://<litellm-alb>
  export ANTHROPIC_API_KEY=<litellm-key>
  export LITELLM_MODEL=<gateway-alias>          # e.g. claude-opus-4-8
  python tests/smoke_model_backend.py

  # Bedrock Invoke API — no key, just AWS credentials with bedrock:InvokeModel*
  export MODEL_BACKEND=bedrock AWS_REGION=us-west-2
  export BEDROCK_MODEL=global.anthropic.claude-opus-5    # optional
  python tests/smoke_model_backend.py

  # Bedrock Mantle endpoint (native Anthropic shape, `anthropic.`-prefixed ids)
  export MODEL_BACKEND=mantle AWS_REGION=us-east-1
  export MANTLE_MODEL=anthropic.claude-opus-5            # optional
  python tests/smoke_model_backend.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

# The agent app is not an installed package from here, so import its backend
# resolver by path — sharing it is the point of this test.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "larkclaudetag" / "app" / "larktag"))
import model_backend  # noqa: E402


@tool("get_secret_number", "Return a secret number. Call this to get it.", {})
async def get_secret_number(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "The secret number is 42."}]}


async def main():
    backend = model_backend.resolve()
    print(f"[backend] {backend.name} | model: {backend.model}")

    server = create_sdk_mcp_server(name="t", version="0.0.1", tools=[get_secret_number])
    opts = ClaudeAgentOptions(
        model=backend.model,
        mcp_servers={"t": server},
        allowed_tools=["mcp__t__get_secret_number"],
        max_turns=5,
        env=dict(backend.env),
    )
    cli = shutil.which("claude")
    if cli:
        opts.cli_path = cli  # force system CLI (bundled binary ignores ANTHROPIC_BASE_URL)

    tool_called = False
    text = ""
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("Use your tool to fetch the secret number, then tell me what it is.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text += b.text
                    elif isinstance(b, ToolUseBlock):
                        tool_called = True
                        print(f"[tool_use] {b.name}")

    print("\n--- reply ---\n", text)
    print("\nRESULT:", "PASS (tool_use fired)" if tool_called else "FAIL (no tool_use — function calling may be dropped)")


if __name__ == "__main__":
    asyncio.run(main())
