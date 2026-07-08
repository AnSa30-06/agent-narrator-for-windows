"""Turn a batch of NarratorEvents into ONE short spoken line.

Works with OpenAI (or any OpenAI-compatible endpoint like OpenRouter) and
Google Gemini, plus a zero-key template fallback used by --no-llm and as the
safety net when the API misbehaves.

make_summarizer() returns a plain function summarize(events) -> str. It keeps
a little state in a closure: the previous line (so the model doesn't repeat
itself) and a failure count (so a flaky API doesn't kill the narrator).
"""

from __future__ import annotations

import os
import sys

from . import parser as ev

SYSTEM_PROMPT = (
    "You narrate a coding agent's work out loud, like a calm radio host. "
    "You receive a short batch of events from the agent's session. "
    "Reply with exactly ONE sentence, under 20 words, present tense, plain "
    "spoken English. No code snippets, no markdown, no surrounding quotes. "
    "If the batch contains an error or the agent needs the user's input, "
    "lead with that. Never repeat the previous line."
)

MAX_FAILURES = 3


def _events_block(events):
    lines = []
    for e in events:
        tool = f" ({e['tool']})" if e["tool"] else ""
        lines.append(f"[{e['kind']}]{tool} {e['detail'][:200]}".rstrip())
    return "\n".join(lines)


def _user_prompt(block, prev_line):
    prev = f'Previous spoken line: "{prev_line}"\n' if prev_line else ""
    return f"{prev}Events:\n{block}\n\nNarrate this batch as one spoken line."


# ---------------------------------------------------------------------------
# Template fallback — no key, no network, always works.
# ---------------------------------------------------------------------------

_TOOL_VERBS = {
    "Bash": "Running a command",
    "PowerShell": "Running a command",
    "Read": "Reading a file",
    "Write": "Writing a file",
    "Edit": "Editing a file",
    "Grep": "Searching the code",
    "Glob": "Looking for files",
    "WebSearch": "Searching the web",
    "WebFetch": "Fetching a page",
}


def _first_words(text, n=14):
    words = text.split()
    out = " ".join(words[:n])
    return out + ("…" if len(words) > n else "")


def template_line(events):
    """Deterministic one-liner straight from the events. Free demo mode."""
    if not events:
        return ""
    K = ev.KINDS
    # Highest-signal event wins: error > attention > last call/text.
    err = next((e for e in events if e["kind"] == K["ERROR"]), None)
    if err is not None:
        what = f" while using {err['tool']}" if err["tool"] else ""
        return f"Heads up — something failed{what}: {_first_words(err['detail'], 10)}"
    attn = next((e for e in events if e["needs_attention"]), None)
    if attn is not None:
        return f"The agent needs you: {_first_words(attn['detail'])}"
    pick = next(
        (e for e in reversed(events) if e["kind"] in (K["TOOL_CALL"], K["ASSISTANT_TEXT"],
                                                      K["USER_MESSAGE"], K["SESSION"], K["DONE"])),
        events[-1],
    )
    extra = len(events) - 1
    suffix = f", plus {extra} more step{'s' if extra > 1 else ''}" if extra > 0 else ""
    kind = pick["kind"]
    if kind == K["TOOL_CALL"]:
        verb = _TOOL_VERBS.get(pick["tool"] or "", f"Using {pick['tool']}")
        detail = pick["detail"].split(":", 1)[-1].strip()
        return f"{verb}: {_first_words(detail, 10)}{suffix}"
    if kind == K["ASSISTANT_TEXT"]:
        return f"The agent says: {_first_words(pick['detail'])}{suffix}"
    if kind == K["USER_MESSAGE"]:
        return f"You told the agent: {_first_words(pick['detail'])}"
    if kind in (K["SESSION"], K["DONE"]):
        return pick["detail"]
    return f"Working: {_first_words(pick['detail'], 10)}{suffix}"


# ---------------------------------------------------------------------------
# LLM callers — each returns a call(block, prev_line) -> str function.
# ---------------------------------------------------------------------------


def _openai_caller(model=None):
    """OpenAI, or any OpenAI-compatible endpoint. Key: OPENAI_API_KEY, else
    OPENROUTER_API_KEY (which also sets the OpenRouter base URL + model name)."""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key and os.getenv("OPENROUTER_API_KEY"):
        api_key = os.getenv("OPENROUTER_API_KEY")
        base_url = base_url or "https://openrouter.ai/api/v1"
    if not api_key:
        raise RuntimeError(
            "No OPENAI_API_KEY (or OPENROUTER_API_KEY) set — "
            "add one to .env or use --no-llm."
        )
    default_model = "gpt-4o-mini"
    if base_url and "openrouter" in base_url:
        default_model = "openai/gpt-4o-mini"
    model = model or os.getenv("OPENAI_MODEL") or default_model
    client = OpenAI(api_key=api_key, base_url=base_url)

    def call(block, prev_line):
        resp = client.chat.completions.create(
            model=model,
            max_tokens=60,
            temperature=0.7,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(block, prev_line)},
            ],
        )
        return resp.choices[0].message.content or ""

    return call


def _gemini_caller(model=None):
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("No GEMINI_API_KEY set — add one to .env or use --no-llm.")
    model = model or os.getenv("GEMINI_MODEL") or "gemini-2.0-flash"
    client = genai.Client(api_key=api_key)

    def call(block, prev_line):
        resp = client.models.generate_content(
            model=model,
            contents=_user_prompt(block, prev_line),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=60,
                temperature=0.7,
            ),
        )
        return resp.text or ""

    return call


def make_summarizer(provider="auto"):
    """provider: auto | openai | gemini | none. Returns summarize(events)->str."""
    if provider == "none":
        template_line.name = "template"
        return template_line

    if provider == "openai":
        call = _openai_caller()
        name = "openai"
    elif provider == "gemini":
        call = _gemini_caller()
        name = "gemini"
    else:  # auto — prefer OpenAI-compatible, then Gemini, else templates
        if os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY"):
            call, name = _openai_caller(), "openai"
        elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            call, name = _gemini_caller(), "gemini"
        else:
            print("[narrator] No API key found — narrating with built-in "
                  "templates. Set OPENAI_API_KEY or GEMINI_API_KEY for smoother "
                  "lines.", file=sys.stderr)
            template_line.name = "template"
            return template_line

    # Wrap the caller with previous-line memory and failure fallback.
    state = {"prev": "", "fails": 0}

    def summarize(events):
        if not events:
            return ""
        if state["fails"] >= MAX_FAILURES:
            return template_line(events)
        try:
            line = (call(_events_block(events), state["prev"]) or "").strip().strip('"')
            state["fails"] = 0
        except Exception as exc:  # never let the narrator die
            state["fails"] += 1
            print(f"[narrator] {name} error ({exc!r}); using template line.",
                  file=sys.stderr)
            if state["fails"] == MAX_FAILURES:
                print(f"[narrator] {name} failed {MAX_FAILURES}x — switching to "
                      "template mode.", file=sys.stderr)
            line = template_line(events)
        line = " ".join(line.split())
        if len(line) > 220:
            line = line[:219] + "…"
        state["prev"] = line
        return line

    summarize.name = name
    return summarize
