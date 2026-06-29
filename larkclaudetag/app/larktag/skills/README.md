# Vendored Claude Code skills

These skill folders are loaded by the `claude` CLI at runtime from
`/app/.claude/skills` (the Dockerfile copies this dir there; `main.py` sets
`cwd=/app` + `setting_sources=["project"]`).

| Skill | Purpose |
|-------|---------|
| `pptx` / `docx` / `xlsx` / `pdf` | document-skills — produce Office/PDF deliverables (run via in-container python: python-pptx/docx/openpyxl/pypdf) |
| `frontend-design` | distinctive web/frontend output |

**Provenance:** the `anthropic-agent-skills` marketplace
(`~/.claude/plugins/marketplaces/anthropic-agent-skills/skills/<name>`).
These are Claude Code-style skills (`SKILL.md` + scripts) that execute locally
via the container's bash/python — unaffected by the LiteLLM→Bedrock
pseudo-passthrough (unlike the Messages-API `container.skills`, which needs betas
Bedrock drops).

## ⚠️ Not in this repository — you must supply them

The `docx/`, `pptx/`, `xlsx/`, `pdf/`, and `frontend-design/` folders are
**Anthropic Proprietary** (see each skill's `LICENSE.txt`). They are **not
redistributed** here — they are git-ignored. Only this README is tracked.

Before building the runtime image (`agentcore deploy`), populate them from your
own Claude Code installation, e.g.:

```bash
SKILLS_SRC=~/.claude/plugins/marketplaces/anthropic-agent-skills/skills
DEST=larkclaudetag/app/larktag/skills
for s in docx pptx xlsx pdf frontend-design; do
  cp -R "$SKILLS_SRC/$s" "$DEST/$s"
done
```

The Docker build copies whatever is present here into `/app/.claude/skills`. If a
folder is missing, the corresponding document/frontend capability is simply
unavailable at runtime; the agent otherwise starts normally.
