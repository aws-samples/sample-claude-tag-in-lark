"""Generate docs/architecture.png with AWS service icons.

Render:  pip install diagrams  +  graphviz (`brew install graphviz`)
         python docs/architecture.py    # writes docs/architecture.png

The diagram is committed alongside this generator so contributors don't need the
toolchain just to read the docs.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2, Fargate, Lambda
from diagrams.aws.database import Database, Dynamodb
from diagrams.aws.integration import Eventbridge
from diagrams.aws.ml import Bedrock
from diagrams.aws.network import APIGateway
from diagrams.aws.security import SecretsManager
from diagrams.aws.storage import S3
from diagrams.onprem.client import Client, Users

graph_attr = {
    "fontsize": "20",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "spline",
    "nodesep": "0.6",
    "ranksep": "1.1",
}
edge_attr = {"fontsize": "11"}

# One color per flow so the three paths read apart at a glance.
SCHED = "darkgreen"   # scheduling: 1-minute heartbeat
AMBIENT = "#1565C0"   # ambient consolidation: hourly sweep

with Diagram(
    "lark-claude-tag — architecture",
    filename="docs/architecture",
    direction="LR",
    show=False,
    graph_attr=graph_attr,
    edge_attr=edge_attr,
):
    lark = Users("Lark group\n(@bot · text/image)")

    with Cluster("Model & external"):
        litellm = EC2("LiteLLM gateway\n(EKS)")
        bedrock = Bedrock("Bedrock\nClaude Opus 4.8 + Haiku")
        exa = Client("Exa · AWS Knowledge\n(MCP)")
        litellm >> Edge(label="Anthropic /v1/messages") >> bedrock

    with Cluster("AWS · us-west-2"):
        with Cluster("Webhook tier (public)"):
            apigw = APIGateway("API Gateway\nHTTP API")
            webhook = Lambda("Webhook Lambda\nverify · decrypt · dedup · ack")

        with Cluster("Agent tier (VPC)"):
            runtime = Fargate("AgentCore Runtime\nClaude Agent SDK")
            memory = Database("AgentCore Memory\nper-channel facts")
            skills = S3("S3\nlearned skills")

        with Cluster("Scheduling (1-min heartbeat)"):
            eventbridge = Eventbridge("EventBridge\nrate(1 minute)")
            dispatcher = Lambda("Dispatcher\nclaim & deliver due jobs")
            schedules = Dynamodb("DynamoDB\nschedules")

        with Cluster("Ambient consolidation (hourly)"):
            eb_hourly = Eventbridge("EventBridge\nrate(1 hour)")
            consolidator = Lambda("Consolidator\ndistill non-@ messages")
            cursor = Dynamodb("DynamoDB\nsweep cursor")

        secrets = SecretsManager("Secrets Manager\nruntime + Lark creds")

    # --- Reactive path (gray): @-mention -> reply ---
    lark >> Edge(label="im.message.receive_v1") >> apigw >> webhook
    webhook >> Edge(label="invoke_agent_runtime\n(session = chat_id)") >> runtime
    runtime >> Edge(label="model calls") >> litellm
    runtime >> Edge(label="recall / remember") >> memory
    runtime >> Edge(label="sync / save_skill") >> skills
    runtime >> Edge(label="web search") >> exa
    runtime >> Edge(label="SSE deltas", style="dashed") >> webhook
    webhook >> Edge(label="CardKit reply (in thread)", style="dashed") >> lark

    # --- Scheduling path (green): heartbeat -> deliver ---
    runtime >> Edge(label="schedule_task / list / cancel", color=SCHED) >> schedules
    eventbridge >> Edge(color=SCHED) >> dispatcher
    dispatcher >> Edge(label="query due", color=SCHED) >> schedules
    dispatcher >> Edge(label="_scheduled_task", color=SCHED) >> webhook

    # --- Ambient path (blue): hourly sweep -> distill non-@ chatter into memory ---
    webhook >> Edge(label="enroll chat (first use)", color=AMBIENT, style="dashed") >> cursor
    eb_hourly >> Edge(color=AMBIENT) >> consolidator
    consolidator >> Edge(label="read / advance cursor", color=AMBIENT) >> cursor
    consolidator >> Edge(label="pull non-@ messages", color=AMBIENT) >> lark
    consolidator >> Edge(label="distill (Haiku)", color=AMBIENT) >> bedrock
    consolidator >> Edge(label="write distilled facts", color=AMBIENT) >> memory

    # --- Config (no secrets in the image) ---
    webhook >> Edge(style="dotted", color="gray") >> secrets
    runtime >> Edge(style="dotted", color="gray") >> secrets
    consolidator >> Edge(style="dotted", color="gray") >> secrets
