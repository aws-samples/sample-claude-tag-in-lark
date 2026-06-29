# Pinned to a specific digest for reproducible builds. To update the base image:
#   docker pull ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# then replace the digest below with the one it reports.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ARG UV_DEFAULT_INDEX
ARG UV_INDEX

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=1 \
    AGENT_APP_DIR=/app \
    AWS_REGION=us-west-2 \
    AWS_DEFAULT_REGION=us-west-2 \
    RUNTIME_SECRET_ID=lark-claude-tag/runtime \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_INDEX=${UV_INDEX} \
    PATH="/app/.venv/bin:$PATH" \
    NODE_PATH="/usr/lib/node_modules" \
    PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"

# pipefail so a failure anywhere in a piped RUN (e.g. the NodeSource `curl | bash`
# below) fails the build instead of being masked by the last command's exit code.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Node.js 20 + Claude Code CLI — the Claude Agent SDK spawns the `claude` CLI as
# a subprocess. We install the system CLI (main.py forces cli_path to it, since
# the SDK's bundled binary ignores ANTHROPIC_BASE_URL).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# document-skills (pptx/docx/pdf) toolchain:
#  - LibreOffice (soffice): pptx/docx -> PDF/thumbnail rendering + xlsx
#  - poppler-utils (pdftoppm): PDF rasterization; fonts-liberation: Arial-like fonts
#  - node libs for html2pptx (pptxgenjs/playwright/sharp/react-icons), globally
#    installed so the skill's `node html2pptx.js` resolves them via NODE_PATH
#  - Chromium for Playwright (HTML render) installed with its system deps into
#    PLAYWRIGHT_BROWSERS_PATH, made world-readable for the runtime user
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice poppler-utils fonts-liberation \
 && npm install -g pptxgenjs playwright sharp react-icons \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 bedrock_agentcore

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=bedrock_agentcore:bedrock_agentcore . .
RUN uv sync --frozen --no-dev

# Claude Code skills load from ./.claude/skills (main.py sets cwd=/app + setting_sources=["project"])
RUN mkdir -p /app/.claude && cp -r /app/skills /app/.claude/skills \
 && chown -R bedrock_agentcore:bedrock_agentcore /app/.claude

USER bedrock_agentcore

# AgentCore Runtime service contract ports
# 8080: HTTP Mode | 8000: MCP Mode | 9000: A2A Mode
EXPOSE 8080 8000 9000

# checkov:skip=CKV_DOCKER_2: AgentCore Runtime manages container health via its own
# GET /ping HTTP contract (polled by the platform), not the Docker HEALTHCHECK
# directive — a Docker-level probe is redundant here and is not consumed by the runtime.
CMD ["opentelemetry-instrument", "python", "-m", "main"]
