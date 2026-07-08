"""Normalize raw Claude Code session events into narrator events.

An event is a plain dict: {"kind", "tool", "detail", "needs_attention"}.
Build one with event(); the kind strings live in KINDS.

Schema (verified against real session logs, 2026-07):
- Top-level "type" values seen: user, assistant, attachment, ai-title, mode,
  queue-operation, last-prompt, system — anything unknown is skipped.
- Assistant events: message.content is a list of blocks typed text, thinking
  (never narrated) or tool_use (name + input; Bash-style tools carry a
  human-readable input.description).
- Tool results arrive as type "user" events whose content blocks are
  tool_result with tool_use_id and an optional is_error.
- A plain-string message.content on a user event — or a text block — means the
  human typed something.
- isSidechain: true marks subagent traffic (skipped).
"""

from .watcher import SESSION_SWITCH

# Event kinds — the "kind" field of every event.
KINDS = {
    "ASSISTANT_TEXT": "assistant_text",
    "TOOL_CALL": "tool_call",
    "TOOL_RESULT": "tool_result",
    "USER_MESSAGE": "user_message",
    "ERROR": "error",
    "ATTENTION": "attention",
    "DONE": "done",
    "SESSION": "session_switch",
}

_SKIP_TYPES = {
    "attachment",
    "ai-title",
    "mode",
    "queue-operation",
    "last-prompt",
    "system",
    "summary",
}

_ATTENTION_MARKERS = (
    "waiting",
    "approve",
    "approval",
    "permission",
    "blocked",
    "do you want",
    "should i",
    "let me know",
    "which option",
    "your call",
)

# input keys worth reading aloud, in preference order, when a tool has no
# human-written description.
_INPUT_HINT_KEYS = (
    "description",
    "command",
    "file_path",
    "path",
    "pattern",
    "query",
    "prompt",
    "url",
    "subject",
    "skill",
)


def event(kind, tool=None, detail="", needs_attention=False):
    return {"kind": kind, "tool": tool, "detail": detail,
            "needs_attention": needs_attention}


def new_state():
    """Per-session parser state: maps tool_use_id -> (tool name, description)."""
    return {"tools": {}}


def _clip(text, limit=500):
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_tool_call(name, inp):
    for key in _INPUT_HINT_KEYS:
        val = inp.get(key)
        if val:
            return f"{name}: {val}"
    return name


def _stringify_result(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = " ".join(p for p in parts if p)
    else:
        return ""
    # Harness wraps some errors in XML-ish tags — not worth reading aloud.
    return text.replace("<tool_use_error>", "").replace("</tool_use_error>", "").strip()


def _looks_like_attention(text):
    low = text.lower()
    return text.rstrip().endswith("?") or any(m in low for m in _ATTENTION_MARKERS)


def parse(raw, state):
    """Map one raw session event to zero or more events (dicts)."""
    events = []
    if not isinstance(raw, dict):
        return events
    if raw.get("isSidechain"):
        return events

    rtype = raw.get("type")

    if rtype == SESSION_SWITCH:
        project = raw.get("project") or "a new session"
        return [event(KINDS["SESSION"], detail=f"Switched to a new session: {project}")]

    if rtype in _SKIP_TYPES:
        return events

    msg = raw.get("message")
    if not isinstance(msg, dict):
        return events

    if rtype == "assistant":
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                attn = _looks_like_attention(text)
                events.append(event(
                    KINDS["ATTENTION"] if attn else KINDS["ASSISTANT_TEXT"],
                    detail=_clip(text),
                    needs_attention=attn,
                ))
            elif btype == "tool_use":
                name = block.get("name") or "a tool"
                detail = _describe_tool_call(name, block.get("input") or {})
                state.setdefault("tools", {})[block.get("id")] = (name, detail)
                events.append(event(KINDS["TOOL_CALL"], tool=name,
                                    detail=_clip(detail, 300)))
            # "thinking" blocks: internal reasoning — never narrate.

    elif rtype == "user":
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
            # Skip harness-injected wrappers (<local-command-...>, <system-reminder>, ...)
            if text and not text.startswith("<"):
                events.append(event(KINDS["USER_MESSAGE"], detail=_clip(text)))
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_result":
                    name, call_detail = state.get("tools", {}).get(
                        block.get("tool_use_id"), (None, "")
                    )
                    is_err = bool(block.get("is_error"))
                    body = _stringify_result(block.get("content")) or call_detail
                    events.append(event(
                        KINDS["ERROR"] if is_err else KINDS["TOOL_RESULT"],
                        tool=name,
                        detail=_clip(body, 300),
                        needs_attention=is_err,
                    ))
                elif btype == "text":
                    text = (block.get("text") or "").strip()
                    if text and not text.startswith("<"):
                        events.append(event(KINDS["USER_MESSAGE"], detail=_clip(text)))

    return events
