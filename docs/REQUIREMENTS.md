# Background & Design

## What this reproduces

[Claude Tag](https://www.anthropic.com/news) is an always-on AI teammate that
lives in Slack channels: @-mention it to delegate a task and it breaks the work
down, uses tools, and replies in-thread. It is Slack-only and gated behind a
Claude Enterprise/Team subscription.

This sample reproduces the core experience on **Lark (Feishu)**, served by a
**LiteLLM gateway** in front of Amazon Bedrock (pseudo-passthrough), so it runs
without a Claude subscription. It also adds a self-evolving memory + skill loop on
top of the original feature set.

## Feature breakdown

The table maps Claude Tag's described capabilities to what this sample implements.

| # | Capability | Claude Tag (as described) | This sample |
|---|------------|---------------------------|-------------|
| 1 | **@-mention delegation** | @Claude in a channel; it decomposes the task → uses tools → replies in-thread. One shared Claude per channel; collaborative, can hand off. | **Implemented** |
| 2 | **Channel memory** | Learns from the channel, remembers key facts so you don't re-explain; with permission can learn across channels/data sources (private channels stay private). | **Implemented** (isolated per channel; no learning across channels it hasn't been added to) |
| 3 | **Ambient mode** (off by default) | Listens to the whole channel without being @-mentioned; proactively nudges and follows up on unfinished threads. | **Partial — ambient perception**: an hourly consolidator sweeps the messages a chat received *without* @-mentioning the bot (since it was first used there) and conservatively distills the durable facts among them (decisions, roles, commitments, stable preferences — chatter dropped) into that channel's memory, tagged `[群聊旁听]`. Perception only; proactive nudges and unprompted follow-ups remain Future. |
| 4 | **Async + self-scheduling** | Runs delegated work in the background for hours/days; schedules its own tasks. | **Implemented (self-scheduling)**: from a chat instruction the bot creates one-shot / recurring / count- or deadline-bounded jobs (`remind` posts text, `agent` runs a turn), fired by a 1-minute EventBridge heartbeat over a DynamoDB registry. Multi-hour background execution is out of scope. |
| 5 | **DM / private chat** | Replies privately in DMs using personal tools/connectors. | Future |
| 6 | **Distinct identity** | Its own identity, with permissions and memory scoped per channel. | Partial (the bot has its own tenant identity; memory is isolated by `chat_id`) |
| 7 | **Tools / connectors** | Connects to codebases, data, and external tools. | **Implemented**: Exa web search + AWS Knowledge/pricing MCP + Lark capabilities (docs/wiki/calendar) + document skills (PPT/Word/Excel/PDF) + frontend design |
| 8 | **Admin governance** | Permission scoping, token budgets (org + channel), audit logs. | Future (out of scope for the sample) |
| 9 | **Runs on Opus** | — | Served via the Opus 4.8 alias on the LiteLLM gateway |

## Acceptance criteria (implemented)

1. @-mention the bot in a group → it replies in-thread (decomposing the task and
   calling tools when needed).
2. It builds context from chat history and remembers key facts across turns within
   the same channel; **memory does not leak across channels**.
3. It can search the web (Exa), create Lark docs / query wiki / check calendar, and
   produce PPT/Word/Excel/PDF deliverables.
4. Full path works end-to-end: Lark group @-mention → AgentCore Runtime (Claude
   Agent SDK + LiteLLM) → in-thread reply.
5. Scheduling: a chat instruction creates a one-shot / recurring / count- or
   deadline-bounded job that fires on time and @-mentions its creator; recurring
   jobs are read back for confirmation before creation, and stop exactly on their
   count/deadline. `list_tasks` / `cancel_task` work; guardrails reject sub-minute
   intervals and over-long/over-frequent jobs.
6. Ambient consolidation: messages a chat received without @-mentioning the bot are
   swept hourly, the durable facts among them are distilled into that channel's
   memory (chatter filtered out, source-tagged `[群聊旁听]`), and the sweep cursor
   advances; a chat with no new messages is a no-op.

## Key technical constraints

- **The LiteLLM backend is a Bedrock provider (pseudo-passthrough).** Built-in
  server-side tools (web search, code execution), the `effort` beta, and the
  deprecated `budget_tokens` parameter are silently dropped. → Use only
  client-side function-calling tools + MCP + Claude Code-style local skills; enable
  no betas; keep thinking adaptive or off.
- **The Claude Agent SDK spawns the `claude` CLI as a subprocess.** The runtime
  needs Node.js + the `claude` binary, and the SDK's bundled binary has a known
  issue where it ignores `ANTHROPIC_BASE_URL` — so force the system CLI with
  `cli_path=shutil.which("claude")`.
- **AgentCore Memory ↔ Agent SDK integration is manual** (no turnkey session
  manager like Strands provides): `RetrieveMemoryRecords` before the turn,
  `CreateEvent` after it (feeding async long-term extraction), and direct
  `BatchCreateMemoryRecords` for explicit "remember this now" facts.
- **Strategy-managed records are not stable storage.** Records in a SEMANTIC
  strategy's namespace can be merged or retired by the service's background
  consolidation at any time (observed: an explicitly written fact disappeared
  after a contradicting conversation, without any client-side delete). Hence
  the memory is layered: user-dictated facts go to a **strategy-free**
  `/actor/{id}/explicit` namespace — still semantically searchable, but only
  removable by an explicit two-phase delete (`forget` returns candidates with
  record ids, `confirm_forget` deletes exactly one confirmed id; never a blind
  top-1 semantic delete). Auto-extracted facts stay in the strategy-managed
  `/facts` namespace as a lower-trust background layer.
- **Memory sessions rotate daily** (`{chat_id}-YYYYMMDD`). An eternal
  per-channel session accumulates one immortal summary that recall keeps
  injecting and no tool can delete; daily rotation bounds summaries and lets
  superseded ones age out of recall.
- **Lark international vs CN:** OpenAPI base is `https://open.larksuite.com`
  (international) or `https://open.feishu.cn` (CN), configurable via `LARK_OPEN_BASE`.
