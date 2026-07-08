"""Transcript sources: where each agent CLI writes its session logs on disk,
and how to turn one raw log line into narrator events.

Supported:
- Claude Code — ~/.claude/projects/**/*.jsonl (schema notes in parser.py)
- Codex CLI  — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

Adding another agent (OpenCode, ...) means adding one class with the same
methods: available(), discover(), new_state(), parse_line(), project_name().

Paths are handled with os / glob and passed around as plain strings.
"""

import glob
import json
import os
from typing import Protocol

from . import parser as claude_parser
from .parser import KINDS, _clip, _looks_like_attention, event
from .watcher import peek_project_name


class Source(Protocol):
    name: str

    def available(self): ...
    def discover(self): ...
    def new_state(self): ...
    def parse_line(self, raw, state): ...
    def project_name(self, path): ...


class ClaudeCodeSource:
    name = "claude"

    def __init__(self, projects_dir=None):
        self.dir = projects_dir or os.path.join(
            os.path.expanduser("~"), ".claude", "projects")

    def available(self):
        return os.path.isdir(self.dir)

    def discover(self):
        if not os.path.isdir(self.dir):
            return []
        hits = glob.glob(os.path.join(self.dir, "**", "*.jsonl"), recursive=True)
        # Subagent transcripts are sidechains — never the main session.
        return [p for p in hits if "subagents" not in p.replace("\\", "/").split("/")]

    def new_state(self):
        return claude_parser.new_state()

    def parse_line(self, raw, state):
        return claude_parser.parse(raw, state)

    def project_name(self, path):
        return peek_project_name(path)


class CodexSource:
    """OpenAI Codex CLI rollout transcripts.

    One JSON object per line: {"timestamp", "type", "payload"}.
    Narration-relevant shapes (unknown types are skipped gracefully):

    - type "session_meta" — payload carries the session cwd.
    - type "response_item" with payload.type:
      - message — role user/assistant, content blocks with text
      - function_call / local_shell_call / custom_tool_call — tool invocation;
        JSON-string arguments or an action.command
      - function_call_output / custom_tool_call_output — result, often a JSON
        string with output + metadata.exit_code
      - reasoning — internal chain of thought: never narrated
    - type "event_msg" with payload.type error / task_complete.
    """

    name = "codex"

    def __init__(self, sessions_dir=None):
        self.dir = sessions_dir or os.path.join(
            os.path.expanduser("~"), ".codex", "sessions")

    def available(self):
        return os.path.isdir(self.dir)

    def discover(self):
        if not os.path.isdir(self.dir):
            return []
        return glob.glob(os.path.join(self.dir, "**", "*.jsonl"), recursive=True)

    def new_state(self):
        return {"tools": {}}

    def project_name(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for _ in range(20):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "session_meta":
                        payload = obj.get("payload") or {}
                        cwd = payload.get("cwd") or (payload.get("meta") or {}).get("cwd")
                        if cwd:
                            return os.path.basename(cwd)
        except OSError:
            pass
        base = os.path.splitext(os.path.basename(str(path)))[0]
        return base.replace("rollout-", "codex ")

    def parse_line(self, raw, state):
        events = []
        if not isinstance(raw, dict):
            return events
        rtype = raw.get("type")
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            return events

        if rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message":
                role = payload.get("role")
                parts = [
                    c.get("text") or ""
                    for c in (payload.get("content") or [])
                    if isinstance(c, dict)
                ]
                text = " ".join(p for p in parts if p).strip()
                if not text:
                    return events
                if role == "assistant":
                    attn = _looks_like_attention(text)
                    events.append(event(
                        KINDS["ATTENTION"] if attn else KINDS["ASSISTANT_TEXT"],
                        detail=_clip(text),
                        needs_attention=attn,
                    ))
                elif role == "user" and not text.startswith("<"):
                    events.append(event(KINDS["USER_MESSAGE"], detail=_clip(text)))

            elif ptype in ("function_call", "local_shell_call", "custom_tool_call"):
                name = payload.get("name") or (
                    "shell" if ptype == "local_shell_call" else "a tool"
                )
                detail = name
                args = payload.get("arguments")
                if isinstance(args, str) and args:
                    try:
                        parsed = json.loads(args)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict) and parsed:
                        val = parsed.get("command") or next(iter(parsed.values()), "")
                        if isinstance(val, list):
                            val = " ".join(map(str, val))
                        detail = f"{name}: {val}"
                    else:
                        detail = f"{name}: {args}"
                action = payload.get("action")
                if isinstance(action, dict) and action.get("command"):
                    cmd = action["command"]
                    if isinstance(cmd, list):
                        cmd = " ".join(map(str, cmd))
                    detail = f"{name}: {cmd}"
                state.setdefault("tools", {})[payload.get("call_id")] = (name, detail)
                events.append(event(KINDS["TOOL_CALL"], tool=name,
                                    detail=_clip(detail, 300)))

            elif ptype in ("function_call_output", "custom_tool_call_output"):
                name, call_detail = state.get("tools", {}).get(
                    payload.get("call_id"), (None, "")
                )
                out = payload.get("output")
                text, is_err = "", False
                if isinstance(out, str):
                    text = out
                    try:
                        parsed = json.loads(out)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict):
                        text = str(parsed.get("output") or text)
                        meta = parsed.get("metadata") or {}
                        code = meta.get("exit_code", parsed.get("exit_code"))
                        is_err = code not in (None, 0)
                elif isinstance(out, dict):
                    text = str(out.get("output") or "")
                    meta = out.get("metadata") or {}
                    code = meta.get("exit_code", out.get("exit_code"))
                    is_err = code not in (None, 0)
                events.append(event(
                    KINDS["ERROR"] if is_err else KINDS["TOOL_RESULT"],
                    tool=name,
                    detail=_clip(text or call_detail, 300),
                    needs_attention=is_err,
                ))
            # "reasoning" and anything else: skipped.

        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "error":
                events.append(event(KINDS["ERROR"],
                                    detail=_clip(str(payload.get("message") or "error"), 300),
                                    needs_attention=True))
            elif ptype == "task_complete":
                events.append(event(KINDS["DONE"], detail="Task complete."))

        return events


def make_sources(names, claude_dir=None, codex_dir=None):
    """Build the requested sources, keeping only ones whose dir exists."""
    out = []
    for n in names:
        n = n.strip().lower()
        if n == "claude":
            out.append(ClaudeCodeSource(claude_dir))
        elif n == "codex":
            out.append(CodexSource(codex_dir))
        elif n:
            raise ValueError(f"Unknown source '{n}' (known: claude, codex)")
    return [s for s in out if s.available()]
