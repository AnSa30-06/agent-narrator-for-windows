"""Speak lines out loud, without ever falling behind the agent.

No classes — every backend is a factory function that returns a speak(text)
function with a .name attribute, and the speech queue is make_speech_queue()
which returns an object with say() and close().

Backends
--------
- edge_backend      — FREE Microsoft Edge neural voices (no key, internet). Default.
- openai_tts_backend— online, paid (gpt-4o-mini-tts); only with a real OPENAI_API_KEY.
- pyttsx3_backend   — offline (SAPI5 / NSSpeech / espeak); --local / no-internet fallback.
- sapi_backend      — Windows fallback via System.Speech.
- null_backend      — silence (--mute); lines are still printed by the CLI.

Queue discipline (the real design problem)
------------------------------------------
TTS is slower than the agent, so a worker thread drains a deque:
- a normal line REPLACES queued normal lines from the SAME station (its key) —
  always speak that agent's newest state, and a chatty agent can't erase a
  quiet agent's pending line;
- error/attention lines jump ahead of normal ones, keeping FIFO among themselves;
- if the active backend keeps failing, hot-swap to the fallback backend.
"""

import os
import subprocess
import sys
import tempfile
import threading
from collections import deque
from types import SimpleNamespace


def null_backend():
    def speak(text):
        return
    speak.name = "mute"
    return speak


def pyttsx3_backend(rate=210):
    """Offline TTS. A fresh engine per utterance dodges the known pyttsx3 bug
    where a reused engine goes silent after the first runAndWait()."""
    import pyttsx3  # fail fast if the driver is broken

    pyttsx3.init().stop()

    def speak(text):
        engine = pyttsx3.init()
        try:
            engine.setProperty("rate", rate)
            engine.say(text)
            engine.runAndWait()
        finally:
            engine.stop()

    speak.name = "pyttsx3"
    return speak


def sapi_backend():
    """Windows-only, zero-dependency fallback via System.Speech."""
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = 3; $s.Speak([Console]::In.ReadToEnd())"
    )

    def speak(text):
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            input=text.encode("utf-8"),
            check=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    speak.name = "sapi"
    return speak


def _play_mp3_file(path):
    if sys.platform == "win32":
        # Windows MCI (winmm) decodes mp3 natively — no extra dependencies.
        import ctypes

        winmm = ctypes.windll.winmm
        alias = f"narrator{threading.get_ident()}"

        def mci(cmd):
            err = winmm.mciSendStringW(cmd, None, 0, 0)
            if err:
                raise RuntimeError(f"MCI error {err} running: {cmd}")

        mci(f'open "{path}" type mpegvideo alias {alias}')
        try:
            mci(f"play {alias} wait")
        finally:
            try:
                mci(f"close {alias}")
            except RuntimeError:
                pass
        return
    players = (
        [["afplay", path]]
        if sys.platform == "darwin"
        else [["mpg123", "-q", path],
              ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
              ["mpv", "--no-video", "--really-quiet", path]]
    )
    for cmd in players:
        try:
            subprocess.run(cmd, check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise RuntimeError("no usable mp3 player found (tried afplay/mpg123/ffplay/mpv)")


def edge_backend(voice=None):
    """Free Microsoft Edge neural voices. No key — just internet. Rate +25%
    by default (EDGE_TTS_RATE overrides); pick a voice with EDGE_TTS_VOICE."""
    import edge_tts  # noqa: F401 — fail fast if missing

    voice = voice or os.getenv("EDGE_TTS_VOICE") or "en-US-AriaNeural"
    rate = os.getenv("EDGE_TTS_RATE") or "+25%"

    def speak(text):
        import asyncio

        import edge_tts

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            path = tmp.name
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(path))
            _play_mp3_file(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    speak.name = "edge"
    return speak


def _play_wav_bytes(data):
    if sys.platform == "win32":
        import winsound

        winsound.PlaySound(data, winsound.SND_MEMORY)
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    players = (
        [["afplay", path]]
        if sys.platform == "darwin"
        else [["paplay", path], ["aplay", "-q", path],
              ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]]
    )
    try:
        for cmd in players:
            try:
                subprocess.run(cmd, check=True, timeout=120,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
        raise RuntimeError("no usable audio player found (tried afplay/paplay/aplay/ffplay)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def openai_tts_backend(voice=None, model=None):
    """OpenAI TTS — requires a real OPENAI_API_KEY (OpenRouter has no TTS)."""
    from openai import OpenAI

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OpenAI TTS needs OPENAI_API_KEY.")
    voice = voice or "alloy"
    model = model or os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
    client = OpenAI()  # deliberately ignores OPENAI_BASE_URL overrides

    def speak(text):
        resp = client.audio.speech.create(
            model=model, voice=voice, input=text, response_format="wav"
        )
        _play_wav_bytes(resp.read())

    speak.name = "openai-tts"
    return speak


def make_speech_queue(backend, fallback=None, on_speak=None):
    """Worker-thread speech queue. Returns an object with say() and close().

    on_speak(text, priority) fires the instant a line starts being spoken — the
    CLI prints there, so the terminal stays in sync with the voice and lines
    dropped as stale are never shown.
    """
    dq = deque()
    cv = threading.Condition()
    # Mutable so the worker can hot-swap to the fallback voice on failure.
    vo = {"backend": backend, "fallback": fallback, "closing": False}

    def say(text, priority=False, key=None):
        text = text.strip()
        if not text:
            return
        with cv:
            if priority:
                # Jump ahead of normal lines, keep FIFO among priority lines.
                idx = 0
                for item in dq:
                    if not item[1]:
                        break
                    idx += 1
                dq.insert(idx, (text, True, key))
            else:
                kept = [it for it in dq
                        if it[1] or (key is not None and it[2] != key)]
                dq.clear()
                dq.extend(kept)
                dq.append((text, False, key))
            cv.notify()

    def speak_resilient(text):
        try:
            vo["backend"](text)
        except Exception as exc:  # noqa: BLE001
            print(f"[narrator] TTS backend '{vo['backend'].name}' failed: {exc!r}",
                  file=sys.stderr)
            if vo["fallback"] is not None:
                print(f"[narrator] falling back to '{vo['fallback'].name}' voice.",
                      file=sys.stderr)
                vo["backend"], vo["fallback"] = vo["fallback"], None
                try:
                    vo["backend"](text)
                except Exception as exc2:  # noqa: BLE001
                    print(f"[narrator] fallback TTS failed too: {exc2!r}", file=sys.stderr)

    def run():
        while True:
            with cv:
                while not dq and not vo["closing"]:
                    cv.wait()
                if not dq and vo["closing"]:
                    return
                text, prio, _key = dq.popleft()
            if on_speak is not None:
                try:
                    on_speak(text, prio)
                except Exception:  # noqa: BLE001 — printing must not kill speech
                    pass
            speak_resilient(text)

    thread = threading.Thread(target=run, daemon=True, name="narrator-tts")
    thread.start()

    def close(drain=True, timeout=15.0, final=None):
        """final clears the backlog and speaks only that line (the sign-off)."""
        with cv:
            if final is not None:
                dq.clear()
                dq.append((final.strip(), True, None))
            elif not drain:
                dq.clear()
            vo["closing"] = True
            cv.notify()
        thread.join(timeout=timeout)

    return SimpleNamespace(say=say, close=close)


def make_backend(local=False, mute=False, voice=None):
    """Pick (backend, fallback). Order: OpenAI TTS (real paid key only) ->
    Edge neural (FREE default) -> pyttsx3 offline (--local / last resort)."""
    if mute:
        return null_backend(), None

    def local_backend():
        try:
            return pyttsx3_backend()
        except Exception as exc:  # noqa: BLE001
            if sys.platform == "win32":
                print(f"[narrator] pyttsx3 unavailable ({exc!r}); using Windows SAPI.",
                      file=sys.stderr)
                return sapi_backend()
            raise

    if local:
        return local_backend(), None

    if os.getenv("OPENAI_API_KEY"):
        try:
            try:
                fb = edge_backend(None)  # OpenAI voice names != Edge names
            except Exception:  # noqa: BLE001
                fb = local_backend()
            return openai_tts_backend(voice=voice), fb
        except Exception as exc:  # noqa: BLE001
            print(f"[narrator] OpenAI TTS unavailable ({exc!r}); using free Edge voice.",
                  file=sys.stderr)

    try:
        return edge_backend(voice=voice), local_backend()
    except Exception as exc:  # noqa: BLE001
        print(f"[narrator] Edge TTS unavailable ({exc!r}); using offline voice.",
              file=sys.stderr)
        return local_backend(), None
