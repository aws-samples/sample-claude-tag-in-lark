"""External MCP server wiring: Exa (web search) + AWS Knowledge + AWS pricing.

All are client-side tool calls (survive LiteLLM->Bedrock pseudo-passthrough,
unlike the built-in server-side WebSearch which Bedrock drops).

Returns (mcp_servers, allowed_tools) to merge into ClaudeAgentOptions alongside
the in-process Lark SDK MCP server (see tools.py).

Env:
  EXA_API_KEY                 — required for Exa search
  AWS_KNOWLEDGE_MCP_URL       — default https://knowledge-mcp.global.api.aws
  ENABLE_AWS_PRICING_MCP      — "1" to enable the stdio pricing server (default off:
                                rarely needed for a Lark teammate, and every loaded
                                tool's schema costs tokens on each turn)
"""

import os
import sys


def build_external_mcp() -> tuple[dict, list[str]]:
    servers: dict = {}
    allowed: list[str] = []

    exa_key = os.environ.get("EXA_API_KEY", "")
    if exa_key:
        # Exa hosted MCP (HTTP). Avoids bundling the node exa-mcp-server.
        servers["exa"] = {"type": "http", "url": f"https://mcp.exa.ai/mcp?exaApiKey={exa_key}"}
        allowed.append("mcp__exa")  # server-level allow (all Exa tools)

    knowledge_url = os.environ.get("AWS_KNOWLEDGE_MCP_URL", "https://knowledge-mcp.global.api.aws")
    if knowledge_url:
        servers["aws_knowledge"] = {"type": "http", "url": knowledge_url}
        allowed.append("mcp__aws_knowledge")

    if os.environ.get("ENABLE_AWS_PRICING_MCP", "0") == "1":
        # awslabs pricing MCP as a stdio subprocess (package installed in image).
        servers["aws_pricing"] = {
            "command": sys.executable,
            "args": ["-m", "awslabs.aws_pricing_mcp_server.server"],
        }
        allowed.append("mcp__aws_pricing")

    return servers, allowed
