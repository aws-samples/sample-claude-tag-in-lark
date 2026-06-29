# AgentCore Memory

The agent's long-term memory is a standalone **AgentCore Memory** resource,
created once and referenced by the Runtime via the `AGENTCORE_MEMORY_ID`
environment variable. This is the first thing to create in the deploy sequence
(see [`docs/DEPLOY_RUNBOOK.md`](../../docs/DEPLOY_RUNBOOK.md)).

## Create it

```bash
python create_memory.py     # from this directory, or: python infra/agentcore/create_memory.py
```

`create_memory.py` provisions one Memory with two strategies:

| Strategy | Name | Namespace template | Purpose |
|----------|------|--------------------|---------|
| SEMANTIC | `channel_facts` | `/actor/{actorId}/facts` | durable, per-channel facts (semantic recall) |
| SUMMARIZATION | `channel_summary` | `/actor/{actorId}/session/{sessionId}/summary` | rolling per-session summary |

Both namespaces are templated on `{actorId}` = the Lark `chat_id`, which is what
**isolates memory per channel** — one channel can never recall another's facts.
Raw short-term events are retained for 90 days (`eventExpiryDuration`).

The extraction model uses cross-region inference (`global.` prefix), per AWS
Bedrock conventions. Adjust `REGION`, `NAME`, and `EXTRACTION_MODEL` at the top of
the script for your environment.

## Wire it into the Runtime

The script prints the **Memory ID**. The runtime config secret also needs the
**semantic strategy ID** (so explicitly-written records land in the same
retrievable index); read it from the created Memory:

```bash
aws bedrock-agentcore-control get-memory --memory-id <MEMORY_ID> --region <AWS_REGION> \
  --query "memory.strategies[?name=='channel_facts'].strategyId | [0]" --output text
```

Put both into the runtime config secret as `AGENTCORE_MEMORY_ID` and
`MEMORY_SEMANTIC_STRATEGY_ID` (Step 3 of the deploy guide).

## How the agent uses it

`larkclaudetag/app/larktag/memory.py` integrates this Memory with the Claude Agent
SDK manually (there is no turnkey session manager):

- **Recall** before a turn — semantic search over `channel_facts` + the session
  summary, merged.
- **Write** — `CreateEvent` after each turn feeds async long-term extraction;
  `BatchCreateMemoryRecords` writes explicit "remember this" facts that are
  immediately retrievable.
- **Curate** — `DeleteMemoryRecord` supports explicit forget / supersede.

> **VPC mode:** if the Runtime reaches the AgentCore data plane through a VPC
> interface endpoint, the endpoint policy must allow the `bedrock-agentcore`
> memory actions on this Memory's ARN — not just the Runtime's IAM role.
