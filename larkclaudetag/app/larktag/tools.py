"""Custom tools exposed to the agent via an in-process SDK MCP server.

Lark capabilities, called with the bot's tenant token. Defined with the
Claude Agent SDK @tool decorator and bundled into one SDK MCP server.

Tool name as seen by the model: mcp__lark__<name>  (see allowed_tools in main.py).
"""

import json
import logging
import os

from claude_agent_sdk import create_sdk_mcp_server, tool

import lark_client
import memory
import schedule
import skill_store

logger = logging.getLogger(__name__)

# The current turn's chat_id. Set by main.py before each query (turns are
# serialized), so file-delivery tools know which chat to post to WITHOUT the
# model having to pass a chat_id it was never told.
_current_chat_id = ""

# The current turn's sender open_id (who's talking). Same serialized-turn trick as
# _current_chat_id — lets schedule_task record who to @-mention when a reminder
# fires, without the model handling an id it was never shown. "" if unknown.
_current_user = ""

# Set when the agent saves/updates a skill mid-turn. main.py reads this to force a
# warm-client rebuild on the NEXT turn so the `claude` CLI re-discovers skills
# (skills are only scanned at CLI launch). Kept here (not in main) to avoid a
# circular import — main imports tools, not the other way around.
_skills_dirty = False


def set_current_chat(chat_id: str) -> None:
    global _current_chat_id
    _current_chat_id = chat_id


def set_current_user(open_id: str) -> None:
    global _current_user
    _current_user = open_id or ""


def skills_dirty() -> bool:
    return _skills_dirty


def clear_skills_dirty() -> None:
    global _skills_dirty
    _skills_dirty = False


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _err(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}], "is_error": True}


@tool("read_chat_history", "读取本群最近的聊天记录,用于补充上下文。参数 chat_id 为当前群 ID,n 为条数(默认20)。", {"chat_id": str, "n": int})
async def read_chat_history(args: dict) -> dict:
    chat_id = args.get("chat_id", "")
    n = int(args.get("n", 20) or 20)
    try:
        items = lark_client.list_chat_messages(chat_id, page_size=n)
    except Exception as e:
        logger.exception("read_chat_history failed")
        return _err(f"读取群历史失败:{e}")
    lines = []
    for it in items:
        body = it.get("body", {}).get("content", "")
        sender = it.get("sender", {}).get("id", "?")
        try:
            txt = json.loads(body).get("text", body)
        except Exception:
            txt = body
        lines.append(f"- [{sender}] {txt}")
    return _text("\n".join(lines) if lines else "(没有历史消息)")


@tool("create_lark_doc", "在 Lark 里新建一个文档,返回文档链接。参数 title 标题,markdown 正文。", {"title": str, "markdown": str})
async def create_lark_doc(args: dict) -> dict:
    try:
        url = lark_client.create_doc(args.get("title", "Untitled"), args.get("markdown", ""))
    except Exception as e:
        logger.exception("create_lark_doc failed")
        return _err(f"建文档失败:{e}")
    return _text(f"已创建文档:{url}")


@tool("search_wiki", "搜索本租户的 Lark 知识库。参数 query 关键词。", {"query": str})
async def search_wiki(args: dict) -> dict:
    try:
        items = lark_client.search_wiki(args.get("query", ""))
    except Exception as e:
        logger.exception("search_wiki failed")
        return _err(f"搜知识库失败:{e}")
    if not items:
        return _text("(没有匹配的知识库内容)")
    lines = [f"- {it.get('title','')}: {it.get('url','')}" for it in items]
    return _text("\n".join(lines))


@tool("query_calendar", "查日历事件。参数 start_iso / end_iso 为 ISO8601 时间窗。", {"start_iso": str, "end_iso": str})
async def query_calendar(args: dict) -> dict:
    try:
        items = lark_client.query_calendar(args.get("start_iso", ""), args.get("end_iso", ""))
    except Exception as e:
        logger.exception("query_calendar failed")
        return _err(f"查日历失败:{e}")
    if not items:
        return _text("(该时间段没有日程)")
    lines = [f"- {it.get('summary','(无标题)')}: {it.get('start_time',{})}" for it in items]
    return _text("\n".join(lines))


@tool(
    "deliver_file",
    "把容器里生成好的文件(PPT/Word/Excel/PDF/图片等)发送到当前群。在你用 skill 产出文件后调用它,用户才能拿到文件。参数 path 为文件的绝对路径,title 可选(发送前的一句说明)。",
    {"path": str, "title": str},
)
async def deliver_file(args: dict) -> dict:
    path = args.get("path", "")
    if not path or not os.path.isfile(path):
        return _err(f"找不到文件:{path}(请确认 skill 已把文件写到这个绝对路径)")
    if not _current_chat_id:
        return _err("当前没有可投递的群上下文,无法发送文件。")
    try:
        lark_client.send_file_to_chat(_current_chat_id, path)
    except Exception as e:
        logger.exception("deliver_file failed")
        return _err(f"文件发送失败:{e}")
    name = os.path.basename(path)
    return _text(f"已把文件「{name}」发送到群里。")


# ---------------------------------------------------------------------------
# Memory tools — the agent actively curates its own per-channel memory.
# ---------------------------------------------------------------------------

@tool(
    "remember",
    "把一件值得长期记住的事(事实/偏好/约定)写入这个群的长期记忆,之后会自动想起来。"
    "用户明确让你记时一定调用;你自己判断某事稳定可复用时也可以调用。参数 fact 为要记住的一句话。",
    {"fact": str},
)
async def remember(args: dict) -> dict:
    fact = (args.get("fact", "") or "").strip()
    if not fact:
        return _err("没有要记住的内容。")
    if not _current_chat_id:
        return _err("当前没有群上下文,无法记忆。")
    ok = memory.remember_fact(_current_chat_id, fact)
    return _text(f"好的,我记住了:{fact}") if ok else _err("记忆写入失败,稍后再试。")


@tool(
    "forget",
    "当某条记住的信息过期、说错了或被推翻时,删除最匹配的那条记忆。"
    "若是'X 改成了 Y'这类更新,先 forget(描述旧的 X) 再 remember(新的 Y)。参数 query 描述要忘掉的内容。",
    {"query": str},
)
async def forget(args: dict) -> dict:
    query = (args.get("query", "") or "").strip()
    if not query:
        return _err("没说要忘掉什么。")
    if not _current_chat_id:
        return _err("当前没有群上下文。")
    deleted = memory.forget_fact(_current_chat_id, query)
    return _text(f"已经忘掉:{deleted}") if deleted else _text("没找到匹配的记忆,可能本来就没记。")


# ---------------------------------------------------------------------------
# Self-evolving skill tool — distill a reusable procedure into a global skill.
# ---------------------------------------------------------------------------

@tool(
    "save_skill",
    "把你刚完成的一套可复用的多步做法,沉淀成一个全局技能(所有群共享),下次遇到同类任务直接复用。"
    "只在做法稳定、有复用价值时调用(一次性闲聊别存)。"
    "参数:name 简短英文小写名(如 make-weekly-ppt);description 一句话说明何时该用它;"
    "body 详细步骤(用到哪些工具、关键参数、注意事项,写成可照做的说明)。",
    {"name": str, "description": str, "body": str},
)
async def save_skill(args: dict) -> dict:
    global _skills_dirty
    ok, info = skill_store.save_skill(
        args.get("name", ""), args.get("description", ""), args.get("body", "")
    )
    if not ok:
        return _err(f"技能保存失败:{info}")
    _skills_dirty = True  # main.py rebuilds the warm client next turn so it loads.
    return _text(f"已沉淀技能「{info}」,以后所有群遇到同类任务我都能直接用。")


# ---------------------------------------------------------------------------
# Scheduled tasks / reminders — let the agent set up future, repeating, or
# self-terminating jobs. A dispatcher Lambda fires them; these tools only
# create / list / cancel. Pass times as RELATIVE seconds (see schedule.py).
# ---------------------------------------------------------------------------

@tool(
    "schedule_task",
    "定一个未来的提醒或定时任务,到点由系统自动在本群触发(并 @ 发起人)。"
    "时间都用『相对现在的秒数』传(prompt 里有当前时间,你据此换算):"
    "delay_seconds=距首次触发多少秒;every_seconds=重复间隔秒(0=只一次,最少 60);"
    "count=最多触发几次(-1=不限,仅重复时有意义);until_seconds=多少秒后截止(0=不设)。"
    "mode='remind' 到点直接发 payload 这段文字(纯提醒,不跑你);"
    "mode='agent' 到点让你按 payload 当成新任务现做(如『扫 Bitget 新闻』)。"
    "description 是给人看的简短标题;payload 是提醒文字或要执行的任务指令。"
    "重复类任务建之前先把解析结果回读给用户确认。",
    {
        "description": str, "payload": str, "mode": str,
        "delay_seconds": int, "every_seconds": int, "count": int, "until_seconds": int,
    },
)
async def schedule_task(args: dict) -> dict:
    if not _current_chat_id:
        return _err("当前没有群上下文,无法定任务。")
    ok, info = schedule.create_job(
        chat_id=_current_chat_id,
        creator_open_id=_current_user,
        description=(args.get("description", "") or "").strip(),
        delay_seconds=int(args.get("delay_seconds", 0) or 0),
        every_seconds=int(args.get("every_seconds", 0) or 0),
        count=int(args.get("count", -1) if args.get("count") is not None else -1),
        until_seconds=int(args.get("until_seconds", 0) or 0),
        mode=(args.get("mode", "remind") or "remind"),
        payload=(args.get("payload", "") or "").strip(),
    )
    if not ok:
        return _err(info)  # info is the human-readable reason (guardrail etc.)
    return _text(f"定好了 ✅ 到点我会在群里提醒。(编号 {info[:8]},说『取消 {info[:8]}』或『取消全部提醒』可撤销)")


@tool("list_tasks", "列出本群进行中的提醒/定时任务。无参数。", {})
async def list_tasks(args: dict) -> dict:
    if not _current_chat_id:
        return _err("当前没有群上下文。")
    return _text(schedule.describe_jobs(_current_chat_id))


@tool(
    "cancel_task",
    "取消本群的提醒/定时任务。参数 target:某个任务的编号(list_tasks 里那串短编号),或『全部』取消所有。",
    {"target": str},
)
async def cancel_task(args: dict) -> dict:
    if not _current_chat_id:
        return _err("当前没有群上下文。")
    _n, msg = schedule.cancel_job(_current_chat_id, (args.get("target", "") or "").strip())
    return _text(msg)


# NOTE: search_wiki / query_calendar are intentionally NOT registered — they are
# infeasible with a bot tenant token (wiki/doc search needs a user token; a bot has
# no personal calendar). The function defs are kept for reference but unused.
LARK_TOOLS = [
    read_chat_history, create_lark_doc, deliver_file, remember, forget, save_skill,
    schedule_task, list_tasks, cancel_task,
]

# Names as the model addresses them (mcp__<server>__<tool>)
LARK_ALLOWED = [
    "mcp__lark__read_chat_history",
    "mcp__lark__create_lark_doc",  # needs scope docx:document to actually create
    "mcp__lark__deliver_file",
    "mcp__lark__remember",
    "mcp__lark__forget",
    "mcp__lark__save_skill",
    "mcp__lark__schedule_task",
    "mcp__lark__list_tasks",
    "mcp__lark__cancel_task",
]


def build_lark_mcp_server():
    """In-process SDK MCP server bundling the Lark tools."""
    return create_sdk_mcp_server(name="lark", version="0.1.0", tools=LARK_TOOLS)
