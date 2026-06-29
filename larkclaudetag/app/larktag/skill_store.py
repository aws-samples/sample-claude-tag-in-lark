"""Self-evolving skill store — the agent's procedural memory.

Skills are Claude Code skills (a directory with a SKILL.md). The `claude` CLI
discovers them from /app/.claude/skills at launch. We keep two kinds:

  * VENDORED skills (pptx/docx/xlsx/pdf/frontend-design) — baked into the image,
    never touched here.
  * LEARNED skills — distilled by the agent itself from successful tasks, stored
    GLOBALLY in S3 (shared across all Lark channels) under the `learned/` prefix.

Lifecycle:
  - sync_down() runs ONCE at container startup (called from runtime_config): pull
    every learned skill from S3 into the local skills dir so the CLI picks them up.
  - save_skill() is called by the `save_skill` tool when the agent decides a
    procedure is worth keeping: write it to S3 (durable, global) AND to the local
    dir. main.py then forces a warm-client rebuild so the new skill is usable in
    the SAME session.

S3 layout (bucket = $SKILL_BUCKET, private):
  learned/<skill-name>/SKILL.md
  learned/<skill-name>/scripts/<file>        (optional)

Everything degrades to a no-op (logged) when SKILL_BUCKET is unset — local dev and
the case where the bucket/IAM isn't wired yet both keep working.
"""

import logging
import os
import re

import boto3

logger = logging.getLogger(__name__)

SKILL_BUCKET = os.environ.get("SKILL_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")
S3_PREFIX = "learned/"
LOCAL_SKILLS_DIR = os.environ.get(
    "AGENT_SKILLS_DIR", os.path.join(os.environ.get("AGENT_APP_DIR", "/app"), ".claude/skills")
)

# Skill names become directory + S3 path segments — keep them strictly safe.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=REGION)
    return _s3


def enabled() -> bool:
    return bool(SKILL_BUCKET)


def normalize_name(name: str) -> str:
    """Slugify an arbitrary title into a safe skill name, or '' if unusable."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (name or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:64]
    return slug if _NAME_RE.match(slug) else ""


def _skill_md(name: str, description: str, body: str) -> str:
    desc = (description or "").replace("\n", " ").strip()
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# Startup sync
# ---------------------------------------------------------------------------

def sync_down() -> int:
    """Pull all learned skills from S3 into the local skills dir. Returns the
    number of files written. No-op (returns 0) when SKILL_BUCKET is unset."""
    if not SKILL_BUCKET:
        logger.info("SKILL_BUCKET not set; skipping learned-skill sync")
        return 0
    written = 0
    try:
        paginator = _client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=SKILL_BUCKET, Prefix=S3_PREFIX):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel = key[len(S3_PREFIX):]  # e.g. "make-weekly-ppt/SKILL.md"
                if not rel:
                    continue
                dest = os.path.join(LOCAL_SKILLS_DIR, rel)
                # Guard against path traversal from a malformed key.
                if not os.path.abspath(dest).startswith(os.path.abspath(LOCAL_SKILLS_DIR) + os.sep):
                    logger.warning("skip unsafe skill key: %s", key)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _client().download_file(SKILL_BUCKET, key, dest)
                written += 1
        logger.info("learned-skill sync: %d files into %s", written, LOCAL_SKILLS_DIR)
    except Exception:
        logger.warning("sync_down failed (continuing with vendored skills only)", exc_info=True)
    return written


# ---------------------------------------------------------------------------
# Save (the self-evolving write path)
# ---------------------------------------------------------------------------

def save_skill(name: str, description: str, body: str, scripts: dict | None = None) -> tuple[bool, str]:
    """Persist a learned skill to S3 (global) and the local skills dir.

    scripts: optional {filename: text_content} written under the skill's scripts/.
    Returns (ok, normalized_name_or_error). Same-name save overwrites = update
    (S3 versioning retains history).
    """
    norm = normalize_name(name)
    if not norm:
        return False, f"技能名不合法:{name!r}(只能小写字母/数字/连字符)"
    if not (body or "").strip():
        return False, "技能内容为空"
    if not SKILL_BUCKET:
        # Still write locally so it's usable this run, but warn it won't persist.
        _write_local(norm, description, body, scripts)
        return True, norm + "(注意:SKILL_BUCKET 未配置,本次有效但不会持久化)"

    md = _skill_md(norm, description, body)
    try:
        _client().put_object(
            Bucket=SKILL_BUCKET,
            Key=f"{S3_PREFIX}{norm}/SKILL.md",
            Body=md.encode("utf-8"),
            ContentType="text/markdown",
        )
        for fn, content in (scripts or {}).items():
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", fn)
            _client().put_object(
                Bucket=SKILL_BUCKET,
                Key=f"{S3_PREFIX}{norm}/scripts/{safe}",
                Body=(content or "").encode("utf-8"),
            )
    except Exception as e:
        logger.warning("save_skill S3 write failed", exc_info=True)
        return False, f"写入 S3 失败:{e}"

    # Mirror locally so it's usable immediately (after a warm-client rebuild).
    _write_local(norm, description, body, scripts)
    return True, norm


def _write_local(name: str, description: str, body: str, scripts: dict | None) -> None:
    try:
        skill_dir = os.path.join(LOCAL_SKILLS_DIR, name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(_skill_md(name, description, body))
        for fn, content in (scripts or {}).items():
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", fn)
            sdir = os.path.join(skill_dir, "scripts")
            os.makedirs(sdir, exist_ok=True)
            with open(os.path.join(sdir, safe), "w", encoding="utf-8") as f:
                f.write(content or "")
    except Exception:
        logger.warning("save_skill local mirror failed", exc_info=True)
