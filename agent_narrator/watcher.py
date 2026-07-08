"""Find and tail the newest Claude Code session JSONL.

Claude Code stores one JSONL file per session under
``~/.claude/projects/<slugified-project-path>/<session-uuid>.jsonl``,
appending one JSON object per line as the session runs.

The tail loop is a simple poll-and-seek: no watchdog dependency, works the
same on Windows, Linux and macOS.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Synthetic event type injected by tail() when it hops to a newer session file.
SESSION_SWITCH = "_narrator_session_switch"


def find_newest_session(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Optional[Path]:
    """Return the most recently modified session JSONL, or None.

    Subagent transcripts (``.../subagents/agent-*.jsonl``) are sidechains and
    are never picked as the main session.
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return None
    candidates = [
        p for p in projects_dir.glob("**/*.jsonl") if "subagents" not in p.parts
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def peek_project_name(path: Path, max_lines: int = 50) -> str:
    """Best-effort human name for the session: the basename of its cwd."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if cwd:
                    return Path(cwd).name
    except OSError:
        pass
    return Path(path).parent.name


def tail(
    path: Path,
    *,
    from_start: bool = False,
    follow: bool = True,
    poll_interval: float = 0.5,
    projects_dir: Optional[Path] = None,
    rescan_interval: float = 3.0,
    switch_after_idle: float = 10.0,
) -> Iterator[Optional[dict]]:
    """Yield parsed JSON events from a session file as they are appended.

    Yields ``None`` on idle polls so callers can run timers (batch flushes,
    idle detection) even when the agent is quiet.

    - ``from_start=False`` seeks to EOF first (live mode).
    - ``follow=False`` stops at EOF instead of polling (replay mode).
    - If ``projects_dir`` is given, a newer session file appearing there makes
      the tail switch to it, emitting a ``{"type": SESSION_SWITCH}`` event —
      but only once the current session has been idle for ``switch_after_idle``
      seconds (two agents running at once would otherwise make the narrator
      ping-pong between their sessions every few polls). The new session is
      joined at its live edge, not replayed from the top.

    Partially-written lines are kept in a buffer until their newline arrives;
    lines that are complete but still fail to parse are skipped.
    """
    path = Path(path)
    f = open(path, encoding="utf-8", errors="replace")
    try:
        if not from_start:
            f.seek(0, os.SEEK_END)
        buf = ""
        last_scan = time.monotonic()
        while True:
            chunk = f.read()
            if chunk:
                buf += chunk
                *complete, buf = buf.split("\n")
                for line in complete:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue  # corrupt line — skip, keep streaming
                continue

            # No new data.
            if not follow:
                tail_line = buf.strip()
                if tail_line:
                    try:
                        yield json.loads(tail_line)
                    except json.JSONDecodeError:
                        pass
                return

            yield None  # idle tick for the caller's timers
            time.sleep(poll_interval)

            # Did the user start a newer session?
            if projects_dir and time.monotonic() - last_scan >= rescan_interval:
                last_scan = time.monotonic()
                newest = find_newest_session(projects_dir)
                try:
                    current_mtime = path.stat().st_mtime
                    switched = (
                        newest is not None
                        and newest != path
                        and newest.stat().st_mtime > current_mtime
                        # hysteresis: don't abandon a session that's still talking
                        and time.time() - current_mtime >= switch_after_idle
                    )
                except OSError:
                    switched = False
                if switched:
                    f.close()
                    path = newest
                    f = open(path, encoding="utf-8", errors="replace")
                    f.seek(0, os.SEEK_END)  # join live — don't replay its history
                    buf = ""
                    yield {
                        "type": SESSION_SWITCH,
                        "path": str(path),
                        "project": peek_project_name(path),
                    }
    finally:
        f.close()
