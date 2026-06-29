# Deployment Guide

End-to-end deployment of the Lark Claude Tag sample. Deploy order matters:

```
Memory  →  Runtime config (Secrets + S3)  →  AgentCore Runtime  →  Webhook Lambda  →  Lark console
```

The Lambda needs the Runtime ARN, and the Runtime reads its config from a secret,
so build the dependencies before the things that consume them.

> All commands run from the repository root unless noted. Replace every
> `<PLACEHOLDER>` with your own value — none of the committed config contains real
> account IDs, ARNs, or endpoints.

---

## Prerequisites

**Tooling (on the machine you deploy from):**

| Tool | Version | Used for |
|------|---------|----------|
| AWS CLI | v2 | credentials, Secrets Manager, S3 |
| AWS SAM CLI | ≥ 1.140 | build/deploy the webhook Lambda |
| AgentCore CLI | latest preview | deploy the Runtime (wraps CDK) |
| Node.js | 20.x | CDK + the `claude` CLI baked into the agent image |
| Python | 3.12 | SAM layer build (`layers/lark-shared`) |
| `uv` | latest | agent dependency resolution |
| Docker | latest | the agent is a Container build (ARM64) |

**Accounts / services you must provide:**

1. **An AWS account + region** with Amazon Bedrock AgentCore available, and
   Bedrock model access enabled for the Claude model you target (the sample uses
   cross-region inference, model id `global.anthropic.claude-opus-4-8`).
2. **An Anthropic-format model endpoint that fronts Bedrock.** The sample was
   built against a **LiteLLM gateway** exposing the `/v1/messages` API, but any
   gateway that speaks the Anthropic Messages API and routes to Bedrock works.
   You need its base URL, an API key, and the model alias it exposes.
3. **A Lark (Feishu) self-built app** — see [Step 7](#step-7--lark-console). You
   need its `app_id`, `app_secret`, `encrypt_key`, and `verification_token`.
4. **An Exa API key** ([exa.ai](https://exa.ai)) for the web-search tool.

**Before you build the agent image — supply the document skills.** The agent
image bundles Claude Code skills from `larkclaudetag/app/larktag/skills/`. The
vendored Anthropic document skills (`docx`, `pptx`, `xlsx`, `pdf`,
`frontend-design`) are **not redistributed in this repository** (proprietary
license) and are git-ignored. Populate them before deploying the Runtime — see
[`larkclaudetag/app/larktag/skills/README.md`](../larkclaudetag/app/larktag/skills/README.md).

---

## Step 1 — Create the AgentCore Memory resource

```bash
python infra/agentcore/create_memory.py     # prints the Memory ID
```

This creates the Memory with a SEMANTIC strategy (`channel_facts`) and a
SUMMARIZATION strategy (`channel_summary`). Namespaces are templated on
`{actorId}` = the Lark `chat_id`, so memory is isolated per channel.

You need **two** values for the runtime config secret (Step 3):

- **Memory ID** — printed by the script.
- **Semantic strategy ID** — read it from the Memory after creation:

  ```bash
  aws bedrock-agentcore-control get-memory --memory-id <MEMORY_ID> \
    --region <AWS_REGION> \
    --query "memory.strategies[?name=='channel_facts'].strategyId | [0]" --output text
  ```

See [`infra/agentcore/README.md`](../infra/agentcore/README.md) for details.

## Step 2 — Create the S3 bucket for self-evolving skills

The agent distills reusable workflows into Claude Code skills and stores them in
S3 (shared across all channels, synced into the container at startup). Create a
**private, versioned** bucket:

```bash
aws s3api create-bucket \
  --bucket <SKILL_BUCKET_NAME> \
  --region <AWS_REGION> \
  --create-bucket-configuration LocationConstraint=<AWS_REGION>
aws s3api put-bucket-versioning \
  --bucket <SKILL_BUCKET_NAME> \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block \
  --bucket <SKILL_BUCKET_NAME> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

## Step 3 — Create the runtime config secret

The Runtime loads its config (including secrets) from one Secrets Manager secret
at startup (`runtime_config.py`), so nothing sensitive is baked into the image.
Create a secret whose JSON value holds:

```json
{
  "ANTHROPIC_BASE_URL": "<your gateway base URL, e.g. http://litellm.internal>",
  "ANTHROPIC_API_KEY": "<your gateway API key>",
  "LITELLM_MODEL": "claude-opus-4-8",
  "AGENTCORE_MEMORY_ID": "<from Step 1>",
  "MEMORY_SEMANTIC_STRATEGY_ID": "<from Step 1>",
  "SKILL_BUCKET": "<SKILL_BUCKET_NAME from Step 2>",
  "SCHEDULE_TABLE_NAME": "lark-claude-tag-schedules",
  "EXA_API_KEY": "<your Exa key>",
  "AWS_KNOWLEDGE_MCP_URL": "https://knowledge-mcp.global.api.aws",
  "LARK_APP_ID": "<Lark app_id>",
  "LARK_APP_SECRET": "<Lark app_secret>",
  "LARK_OPEN_BASE": "https://open.larksuite.com"
}
```

```bash
aws secretsmanager create-secret \
  --name lark-claude-tag/runtime \
  --secret-string file://runtime-config.json   # delete this local file afterwards
```

The container reads it via the `RUNTIME_SECRET_ID` env var (default
`lark-claude-tag/runtime`, set in the Dockerfile). Use `https://open.feishu.cn`
for `LARK_OPEN_BASE` on Feishu CN. `SCHEDULE_TABLE_NAME` points at the DynamoDB
table the SAM stack creates in Step 8 (fixed name `lark-claude-tag-schedules`),
used by the scheduled-tasks tools — set it now so it's present when the image starts.

## Step 4 — Deploy the AgentCore Runtime

1. Set your deploy target in `larkclaudetag/agentcore/aws-targets.json`
   (`account` → your account ID; adjust `region` if needed).
2. Configure networking in `larkclaudetag/agentcore/agentcore.json`:
   - **VPC mode** (as shipped): fill `subnets` with two private subnets and
     `securityGroups` with a security group that allows egress to your gateway,
     Exa, the AWS Knowledge MCP, and Lark. VPC mode is required if your model
     gateway is only reachable inside a VPC.
   - **PUBLIC mode** (simpler first deploy): set `"networkMode": "PUBLIC"` and
     remove the `networkConfig` block. The Runtime then egresses from a managed
     NAT; allowlist that egress on your gateway instead (see Step 6).
3. Deploy:

   ```bash
   cd larkclaudetag/agentcore
   agentcore deploy
   ```

   This builds the container in CodeBuild (ARM64), pushes to ECR, and creates the
   Runtime via CDK. **Record the Runtime ARN** it prints — it's the
   `AgentRuntimeArn` SAM parameter in Step 8.

## Step 5 — Grant the Runtime execution role its permissions

`agentcore deploy` creates the Runtime execution role. Attach a policy (scoped to
**your** resource ARNs) granting:

| Action(s) | Resource | Why |
|-----------|----------|-----|
| `secretsmanager:GetSecretValue` | the runtime config secret | load config at startup |
| `bedrock-agentcore:CreateEvent`, `RetrieveMemoryRecords`, `BatchCreateMemoryRecords`, `ListEvents`, `ListMemoryRecords`, `DeleteMemoryRecord` | the Memory ARN | per-channel memory (recall, explicit remember/forget, re-seed) |
| `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` | the skills bucket (+ `/*`) | self-evolving skill store |
| `dynamodb:PutItem`, `dynamodb:Query`, `dynamodb:UpdateItem` | the schedules table (+ `/index/*`) | scheduled-tasks tools (create / list / cancel) |

> Attach these as a **separate inline policy** so a later `agentcore deploy` (which
> regenerates the CDK-managed default policy) doesn't drop them.

> **VPC mode only:** if the Runtime reaches the AgentCore data plane through a VPC
> interface endpoint, the **endpoint policy** must allow the same
> `bedrock-agentcore` memory actions on the Memory ARN — granting them on the IAM
> role alone is not enough.

## Step 6 — Open egress to your model gateway

The Runtime must reach: your model gateway, `mcp.exa.ai`,
`knowledge-mcp.global.api.aws`, and your `LARK_OPEN_BASE` host. If your gateway
sits behind a security-group-restricted load balancer, add the Runtime's egress
(its NAT IP `/32`, or its security group) to the gateway's inbound rule.

> **Never** open the gateway to `0.0.0.0/0`. Allowlist the specific egress source.

## Step 7 — Validate the Runtime directly (before wiring the Lambda)

Confirm the agent works end-to-end through the gateway before adding the webhook:

```bash
cd larkclaudetag/agentcore
agentcore invoke '{"prompt": "Search the web for the latest AWS news and summarize it."}'
```

You should get streamed text back and see a tool fire (Exa / AWS Knowledge). This
proves function-calling survives the gateway→Bedrock path. Only then continue.

## Step 8 — Deploy the webhook Lambda (SAM)

1. Create the Lark credentials secret the Lambda reads (separate from the runtime
   secret). Its JSON value holds:

   ```json
   {
     "LARK_APP_ID": "<Lark app_id>",
     "LARK_APP_SECRET": "<Lark app_secret>",
     "LARK_ENCRYPT_KEY": "<Lark encrypt_key>",
     "LARK_VERIFICATION_TOKEN": "<Lark verification_token>"
   }
   ```

   ```bash
   aws secretsmanager create-secret \
     --name lark-claude-tag/lark-credentials \
     --secret-string file://lark-credentials.json   # delete afterwards
   ```

2. Fill `samconfig.toml` `parameter_overrides`:
   - `LarkSecretsArn` → the ARN of the secret you just created
   - `AgentRuntimeArn` → the Runtime ARN from Step 4
   - `LarkOpenBase` → `https://open.larksuite.com` (or the CN base)
   - `AgentCoreMemoryId` → the Memory id from Step 1 (same value as the runtime
     secret's `AGENTCORE_MEMORY_ID`) — the ambient consolidator writes into it
   - `MemorySemanticStrategyId` → the SEMANTIC strategy id from Step 1 (same value
     as `MEMORY_SEMANTIC_STRATEGY_ID`)

3. Build and deploy:

   ```bash
   sam build && sam deploy
   ```

   Besides the webhook Lambda + HTTP API, this stack also creates the
   scheduled-tasks infrastructure: the **DynamoDB** table `lark-claude-tag-schedules`,
   the **dispatcher** Lambda, and the **EventBridge** `rate(1 minute)` heartbeat
   that fires it. It likewise creates the **ambient-consolidation** infrastructure:
   the **DynamoDB** cursor table `lark-claude-tag-consolidation`, the
   **consolidator** Lambda, and a separate **EventBridge** `rate(1 hour)` sweep that
   distills non-@ group messages into per-channel memory. The consolidator runs
   under its own SAM-managed role and writes to the Memory ARN built from the
   `AgentCoreMemoryId` parameter, so it needs no extra IAM wiring on the Runtime
   role. Note the **`WebhookUrl`** and **`SchedulesTableArn`** outputs (the latter
   is what you scope the Runtime's DynamoDB inline policy to in Step 5).

4. (Optional) Smoke-test Runtime connectivity from the Lambda, without any Lark
   calls:

   ```bash
   aws lambda invoke --function-name <stack>-LarkWebhookFunction-<suffix> \
     --payload '{"_smoke_test": true}' /dev/stdout
   ```

   A `{"ok": true, "reply_len": ...}` response confirms the Lambda can reach the
   Runtime.

## Step 9 — Lark console

In the Lark/Feishu developer console, on your self-built app:

1. **Event subscription** → Request URL = the `WebhookUrl` from Step 8. Lark sends
   an encrypted `url_verification` challenge; the handler answers it automatically.
2. **Subscribe** to the event `im.message.receive_v1`.
3. **Permissions (scopes):** `im:message`, `im:message:send_as_bot`,
   `im:resource` (image download), `cardkit:card:read`, `cardkit:card:write`
   (streaming reply cards), plus `docx:document` and any doc/wiki/calendar scopes
   your tools use.
4. **Enable the bot** capability, **publish a version**, then add the bot to a
   test group.

## Step 10 — End-to-end verification

In the test group:

1. **@-mention** the bot with a question → it replies in-thread with a streaming
   card that shows which tool is running.
2. **Memory:** tell it a fact (`remember: our project codename is Pluto`), then in
   a new turn ask for it → it recalls. Repeat in a **different group** → it must
   **not** know (per-channel isolation).
3. **Tools/skills:** ask it to search the web (Exa), create a Lark doc, and
   generate a PPT/Word/Excel file → the file is delivered into the chat.
4. **Multimodal:** @-mention with an image attached → it describes the image.

---

## Troubleshooting

- **Agent ignores the gateway / hits the public Anthropic API.** The Claude Agent
  SDK's bundled `claude` binary ignores `ANTHROPIC_BASE_URL`; the image installs
  the system CLI and `main.py` forces `cli_path=shutil.which("claude")`. Confirm
  Node + the CLI are present in the image.
- **Memory calls fail only in VPC mode.** The VPC interface endpoint policy must
  allow the `bedrock-agentcore` memory actions on your Memory ARN (Step 5 note).
- **Gateway connection refused/timeout from the Runtime.** Egress isn't
  allowlisted on the gateway (Step 6), or PUBLIC/VPC networking is misconfigured.
- **`budget_tokens` / server-side tools / `effort` not working.** A Bedrock-backed
  gateway runs pseudo-passthrough and silently drops betas, built-in server tools,
  and `budget_tokens`. Use only client-side tools (MCP / `@tool`) and local
  skills; keep thinking adaptive or off.
