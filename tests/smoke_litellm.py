"""Step-3 smoke test: Claude Agent SDK -> LiteLLM, with a dummy custom tool.

Confirms the riskiest assumption: that client-side function calling survives the
LiteLLM->Bedrock pseudo-passthrough (i.e. the tool is offered and Claude can
call it). If this passes, the agent's MCP/@tool surface will work.

Run locally (needs the system `claude` CLI + network to LiteLLM):
  export ANTHROPIC_BASE_URL=http://<litellm-alb>
  export ANTHROPIC_API_KEY=<litellm-key>
  export LITELLM_MODEL=<gateway-alias>     # e.g. claude-opus-4-8
  python tests/smoke_litellm.py
"""

import asyncio
import os
import shutil

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)


@tool("get_secret_number", "Return a secret number. Call this to get it.", {})
async def get_secret_number(args: dict) -> dict:
    return {"content": [{"type": "text", "text": "The secret number is 42."}]}


async def main():
    model = os.environ.get("LITELLM_MODEL", "claude-opus-4-8")
    server = create_sdk_mcp_server(name="t", version="0.0.1", tools=[get_secret_number])
    opts = ClaudeAgentOptions(
        model=model,
        mcp_servers={"t": server},
        allowed_tools=["mcp__t__get_secret_number"],
        max_turns=5,
        env={
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        },
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
