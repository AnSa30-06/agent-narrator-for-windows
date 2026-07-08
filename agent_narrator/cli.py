"""agent-narrator CLI — tune into your coding agents.

Default mode watches EVERY active session across all sources (Claude Code,
Codex) as parallel "stations": per-station batching and idle announcements,
one shared voice. --session/--replay narrate a single session.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from . import parser as ev
from .multiplexer import StationMux
from .parser import new_state, parse
from .sources import make_sources
from .summarizer import make_summarizer
from .tts import make_backend, make_speech_queue
from .watcher import DEFAULT_PROJECTS_DIR, find_newest_session, peek_project_name, tail

K = ev.KINDS

# Internal tuning — sensible defaults, not worth a flag each.
BATCH_WINDOW = 3.0      # seconds of events gathered per spoken line
MAX_BATCH = 8           # flush early once a batch reaches this many events
ACTIVE_WINDOW = 600.0   # a session counts as live if touched this recently
STALE_TIMEOUT = 900.0   # retire a station after this much silence

# Which event kinds are narrated at each verbosity level.
_VERBOSITY_KINDS = {
    "quiet": {K["ERROR"], K["ATTENTION"], K["SESSION"], K["DONE"]},
    "normal": {K["ERROR"], K["ATTENTION"], K["SESSION"], K["DONE"],
               K["TOOL_CALL"], K["TOOL_RESULT"], K["ASSISTANT_TEXT"]},
    "chatty": {K["ERROR"], K["ATTENTION"], K["SESSION"], K["DONE"],
               K["TOOL_CALL"], K["TOOL_RESULT"], K["ASSISTANT_TEXT"], K["USER_MESSAGE"]},
}

# Min seconds between spoken lines, per verbosity.
_RATE_LIMIT = {"quiet": 0.0, "normal": 5.0, "chatty": 3.0}

# Spoken (and printed) on Ctrl-C. Override with NARRATOR_SIGNOFF in the env/.env.
SIGN_OFF = "Agent FM team, I'd love to work with you if you're interested."


def _emit(line, priority):
    tag = "[!]" if priority else "[>]"
    try:
        print(f"{tag} {line}", flush=True)
    except UnicodeEncodeError:
        print(f"{tag} {line.encode('ascii', 'replace').decode()}", flush=True)


def _build_parser():
    p = argparse.ArgumentParser(
        prog="agent-narrator",
        description="Hear your coding agents work — local-first, BYO-key, cross-platform.",
    )
    p.add_argument("--version", action="version", version=f"agent-narrator {__version__}")
    p.add_argument("--session", help="narrate ONE specific session .jsonl (single-station mode)")
    p.add_argument("--sources", default="claude,codex",
                   help="comma-separated transcript sources to watch (default: claude,codex)")
    p.add_argument("--projects-dir", help="Claude Code projects dir (default: ~/.claude/projects)")
    p.add_argument("--codex-dir", help="Codex sessions dir (default: ~/.codex/sessions)")
    p.add_argument("--stations", type=int, default=6,
                   help="max concurrent sessions to narrate (default: 6)")
    p.add_argument("--provider", choices=["auto", "openai", "gemini", "none"],
                   default="auto", help="LLM provider for narration lines (default: auto)")
    p.add_argument("--no-llm", action="store_true", help="template lines only — no API key needed")
    p.add_argument("--local", action="store_true", help="force fully offline TTS (pyttsx3)")
    p.add_argument("--mute", action="store_true", help="print lines, speak nothing")
    p.add_argument("--voice", help="voice name — Edge neural (e.g. en-US-AriaNeural, default) "
                                   "or OpenAI (e.g. alloy)")
    p.add_argument("--verbosity", choices=["quiet", "normal", "chatty"],
                   default="normal", help="how chatty to be (default: normal)")
    p.add_argument("--replay", action="store_true",
                   help="(single session) read it all from the beginning, then exit")
    p.add_argument("--idle-timeout", type=float, default=25.0,
                   help="seconds of silence before a station is announced idle (default: 25)")
    return p


def main(argv=None):
    """Hear your coding agents work — local-first, BYO-key, cross-platform."""
    args = _build_parser().parse_args(argv)
    load_dotenv()

    if args.session and not Path(args.session).is_file():
        sys.exit(f"agent-narrator: no such session file: {args.session}")

    provider = "none" if args.no_llm else args.provider
    try:
        summarizer = make_summarizer(provider)
    except RuntimeError as exc:
        sys.exit(f"agent-narrator: {exc}")

    backend, fallback = make_backend(local=args.local, mute=args.mute, voice=args.voice)
    # Lines print at the moment they're spoken, so terminal and voice agree.
    speech = make_speech_queue(backend, fallback, on_speak=_emit)

    min_interval = _RATE_LIMIT[args.verbosity]
    narrated_kinds = _VERBOSITY_KINDS[args.verbosity]

    print(f"agent-narrator v{__version__} — voice: {backend.name}, "
          f"summarizer: {getattr(summarizer, 'name', 'template')}, "
          f"verbosity: {args.verbosity}")

    try:
        if args.session or args.replay:
            _run_single(args.session, args.projects_dir, summarizer, speech,
                        narrated_kinds, min_interval, args.idle_timeout, args.replay)
        else:
            _run_stations(args.sources, args.projects_dir, args.codex_dir, args.stations,
                          summarizer, speech, narrated_kinds, min_interval, args.idle_timeout)
    except KeyboardInterrupt:
        print()  # drop off the ^C line
        speech.close(final=os.getenv("NARRATOR_SIGNOFF") or SIGN_OFF, timeout=12.0)


# ---------------------------------------------------------------------------
# Multi-station mode (default)
# ---------------------------------------------------------------------------


def _run_stations(sources_csv, projects_dir, codex_dir, max_stations,
                  summarizer, speech, narrated_kinds, min_interval,
                  idle_timeout):
    try:
        sources = make_sources(sources_csv.split(","), projects_dir, codex_dir)
    except ValueError as exc:
        sys.exit(f"agent-narrator: {exc}")
    if not sources:
        sys.exit("agent-narrator: no transcript sources found. Run Claude Code or "
                 "Codex once, or point --projects-dir/--codex-dir at the right place.")

    mux = StationMux(sources, max_stations=max_stations,
                     active_window=ACTIVE_WINDOW, stale_timeout=STALE_TIMEOUT)
    mux.rescan(force=True)
    if not mux.stations:
        newest = mux.newest_any()
        if newest is None:
            sys.exit("agent-narrator: no sessions found in any source. "
                     "Run your agent at least once.")
        mux.pin(*newest)

    names = ", ".join(st.label for st in mux.stations)
    if len(mux.stations) == 1:
        startup = f"Tuned in to {names}."
    else:
        startup = f"Tuned in to {len(mux.stations)} stations: {names}."
    for st in mux.stations:
        print(f"station: [{st.source.name}] {st.path}")
    speech.say(startup)  # queue is empty at startup — no need to jump it

    last_spoken = 0.0
    poll_interval = 0.5

    def announce(text, key, priority=False):
        speech.say(text, priority=priority, key=key)

    while True:
        now = time.monotonic()

        added, removed = mux.rescan()
        for st in added:
            announce(f"New station: {st.label}.", st.key, priority=True)
        for st in removed:
            announce(f"{st.label} signs off.", st.key)

        pairs = mux.read_all()
        for st, raw in pairs:
            st.last_activity = now
            if st.idle_announced:
                st.idle_announced = False
                announce(f"{st.label} is back at work.", st.key)
            for event in st.source.parse_line(raw, st.state):
                if event["kind"] not in narrated_kinds:
                    continue
                if not st.batch:
                    st.batch_started = now
                st.batch.append(event)

        multi = len(mux.stations) > 1
        for st in mux.stations:
            priority = any(e["needs_attention"] or e["kind"] == K["ERROR"] for e in st.batch)
            if st.batch and (
                priority
                or len(st.batch) >= MAX_BATCH
                or ((now - st.batch_started) >= BATCH_WINDOW
                    and (now - last_spoken) >= min_interval)
            ):
                line = summarizer(st.batch)
                st.batch = []
                st.batch_started = 0.0
                if line:
                    if multi:
                        line = f"{st.label}: {line}"
                    speech.say(line, priority=priority, key=st.key)
                    last_spoken = now

            if (not st.idle_announced and not st.batch
                    and (now - st.last_activity) >= idle_timeout):
                st.idle_announced = True
                announce(f"{st.label} has gone idle.", st.key)

        if not pairs:
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Single-session mode (--session / --replay)
# ---------------------------------------------------------------------------


def _run_single(session_path, projects_dir, summarizer, speech, narrated_kinds,
                min_interval, idle_timeout, replay):
    projects_dir = Path(projects_dir) if projects_dir else DEFAULT_PROJECTS_DIR
    if session_path is None:
        session_path = find_newest_session(projects_dir)
        if session_path is None:
            sys.exit(f"agent-narrator: no Claude Code sessions found under "
                     f"{projects_dir}. Run Claude Code once, or pass --session <path>.")

    project = peek_project_name(session_path)
    follow = not replay

    print(f"session: {session_path}")
    speech.say(f"Tuned in to {project}.")

    state = new_state()
    batch = []
    batch_started = 0.0
    last_spoken = 0.0
    last_activity = time.monotonic()
    announced_done = True  # don't announce "idle" before any activity

    def flush(now, priority):
        nonlocal batch, batch_started, last_spoken
        if not batch:
            return
        line = summarizer(batch)
        if line:
            speech.say(line, priority=priority)
            last_spoken = now
        batch = []
        batch_started = 0.0

    for raw in tail(session_path, from_start=replay, follow=follow,
                    projects_dir=projects_dir if follow else None):
        now = time.monotonic()

        if raw is not None:
            last_activity = now
            if announced_done:
                announced_done = False
                if not replay and raw.get("type") != K["SESSION"]:
                    speech.say(f"{project} is back at work.", priority=False)
            for event in parse(raw, state):
                if event["kind"] not in narrated_kinds:
                    continue
                if not batch:
                    batch_started = now
                batch.append(event)

        priority = any(e["needs_attention"] or e["kind"] in (K["ERROR"], K["SESSION"])
                       for e in batch)
        if batch and (
            priority
            or len(batch) >= MAX_BATCH
            or ((now - batch_started) >= BATCH_WINDOW
                and (now - last_spoken) >= min_interval)
        ):
            flush(now, priority)

        if (follow and not announced_done and not batch
                and (now - last_activity) >= idle_timeout):
            announced_done = True
            speech.say(f"{project} has gone idle.", priority=False)

    flush(time.monotonic(), priority=any(
        e["needs_attention"] or e["kind"] == K["ERROR"] for e in batch))
    speech.close(drain=True)


if __name__ == "__main__":
    main()
