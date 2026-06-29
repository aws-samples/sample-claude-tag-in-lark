"""Create the AgentCore Memory resource for the Lark teammate.

Per-channel isolation: namespace templated on {actorId} (= Lark chat_id).
The agent (larkclaudetag/app/larktag/memory.py) reads/writes this memory at runtime.

Run once:  python infra/agentcore/create_memory.py
Then set AGENTCORE_MEMORY_ID on the Runtime (see infra/agentcore/README.md).

NOTE: confirm the create_memory strategy/namespace shape against the installed
boto3 version — the AgentCore Memory control-plane API is post-knowledge-cutoff.
Extraction models must use cross-region inference with the `global.` prefix
(per the AWS workspace conventions).
"""

import os

import boto3

REGION = "us-west-2"
NAME = "lark_claude_tag_memory"
# Match larkclaudetag/app/larktag/memory.py NAMESPACE_TMPL.
SEMANTIC_NS = "/actor/{actorId}/facts"
SUMMARY_NS = "/actor/{actorId}/session/{sessionId}/summary"
EXTRACTION_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
# Customer-managed KMS key for encryption at rest. Strongly recommended: a
# memory holds plaintext conversation events, and AWS Security Hub control
# BedrockAgentCore.3 flags a memory without a CMK as non-compliant. Set
# MEMORY_CMK_ARN to the CMK ARN (account-specific, so read from env, not
# hard-coded). `encryptionKeyArn` is create-time only — it cannot be added to an
# existing memory via update-memory, so enabling it later means recreating.
CMK_ARN = os.environ.get("MEMORY_CMK_ARN", "")


def main():
    c = boto3.client("bedrock-agentcore-control", region_name=REGION)
    kwargs = dict(
        name=NAME,
        description="Per-channel memory for the Lark Claude Tag teammate (actorId = chat_id).",
        eventExpiryDuration=90,  # days of raw short-term event retention (3-365)
        memoryStrategies=[
            {
                "semanticMemoryStrategy": {
                    "name": "channel_facts",
                    "namespaces": [SEMANTIC_NS],
                }
            },
            {
                "summaryMemoryStrategy": {
                    "name": "channel_summary",
                    "namespaces": [SUMMARY_NS],
                }
            },
        ],
    )
    if CMK_ARN:
        kwargs["encryptionKeyArn"] = CMK_ARN  # CMK-encrypt at rest (recommended)
    else:
        print("WARNING: MEMORY_CMK_ARN not set — memory will use the default AWS-managed key (Security Hub BedrockAgentCore.3 non-compliant).")
    resp = c.create_memory(**kwargs)
    mem = resp.get("memory", resp)
    print("Created memory:", mem.get("id") or mem.get("memoryId") or mem)
    print("-> set AGENTCORE_MEMORY_ID to this id on the AgentCore Runtime.")


if __name__ == "__main__":
    main()
