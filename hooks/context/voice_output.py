"""Voice output — spoken summaries of Claude responses via Anton Voice Service.

Toggle: operator says "enable voice" / "disable voice" in chat.
Pipeline: Stop hook → Haiku summarizes → POST /speak → ffplay plays OGG.
Session-scoped: persists until session end or explicit disable.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log

VOICE_TYPE = "voice_enabled"
_FLAG_DIR = Path.home() / ".agentihooks" / "voice_flags"

_RE_ENABLE = re.compile(r"\b(enable|turn\s+on|activate)\s+voice\b", re.IGNORECASE)
_RE_DISABLE = re.compile(r"\b(disable|turn\s+off|deactivate)\s+voice\b", re.IGNORECASE)
_QUOTA_FLAG = Path.home() / ".agentihooks" / "voice_flags" / "quota_exhausted"


def _write_quota_flag() -> None:
    _QUOTA_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _QUOTA_FLAG.write_text("1")


def _is_quota_exhausted() -> bool:
    return _QUOTA_FLAG.exists()

_SUMMARIZER_SYSTEM = (
    "You are a voice briefing system. Your ONLY job: compress the input into "
    "ONE spoken sentence, maximum 30 words. No markdown. No bullet points. "
    "No code. No lists. Just one clean sentence a human can hear in under "
    "10 seconds. If the input is a short answer, rephrase it naturally for "
    "speech. Never exceed 30 words."
)


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------


def contains_enable_signal(text: str) -> bool:
    return bool(_RE_ENABLE.search(text))


def contains_disable_signal(text: str) -> bool:
    return bool(_RE_DISABLE.search(text))


# ---------------------------------------------------------------------------
# Flag management (session-scoped, no TTL)
# ---------------------------------------------------------------------------


def set_voice_enabled(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.set(redis_key(VOICE_TYPE, session_id), "1")
        except Exception:
            pass
    _FLAG_DIR.mkdir(parents=True, exist_ok=True)
    (_FLAG_DIR / f"{session_id}.voice").write_text("1")
    try:
        _QUOTA_FLAG.unlink(missing_ok=True)
    except Exception:
        pass


def clear_voice_enabled(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(redis_key(VOICE_TYPE, session_id))
        except Exception:
            pass
    try:
        (_FLAG_DIR / f"{session_id}.voice").unlink(missing_ok=True)
    except Exception:
        pass


def is_voice_enabled(session_id: str) -> bool:
    r = get_redis()
    if r:
        try:
            return bool(r.exists(redis_key(VOICE_TYPE, session_id)))
        except Exception:
            pass
    try:
        return (_FLAG_DIR / f"{session_id}.voice").exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Haiku summarizer
# ---------------------------------------------------------------------------


def _summarize_with_haiku(text: str) -> str | None:
    try:
        clamped = text[:1500] if len(text) > 1500 else text
        result = subprocess.run(
            ["claude", "-p", "--bare", "--model", "claude-haiku-4-5", "--system-prompt", _SUMMARIZER_SYSTEM],
            input=clamped,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except FileNotFoundError:
        log("voice_output: claude CLI not found on PATH", {})
        return None
    except subprocess.TimeoutExpired:
        log("voice_output: haiku summarizer timed out", {})
        return None
    except Exception as e:
        log("voice_output: haiku summarizer failed", {"error": str(e)})
        return None


# ---------------------------------------------------------------------------
# TTS + playback
# ---------------------------------------------------------------------------


def _is_wsl2() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _find_ffplay() -> list[str]:
    """Find ffplay binary — WSL2 uses Windows ffplay.exe, native Linux uses ffplay."""
    if _is_wsl2():
        for candidate in [
            "/mnt/c/Tools/ffmpeg-7.0/bin/ffplay.exe",
            "/mnt/c/ProgramData/chocolatey/bin/ffplay.exe",
        ]:
            if Path(candidate).exists():
                return [candidate]
    return ["ffplay"]


def _audio_path_for_player(path: str) -> str:
    """Convert path for the player — WSL2 needs Windows-style path for ffplay.exe."""
    if _is_wsl2():
        try:
            result = subprocess.run(
                ["wslpath", "-w", path], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    return path


def _speak_and_play(text: str, voice_service_url: str) -> None:
    try:
        from hooks.config import VOICE_API_KEY

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        payload = json.dumps({"text": text, "store": False})
        curl_cmd = [
            "curl", "-sf",
            "-X", "POST",
            f"{voice_service_url}/speak",
            "-H", "Content-Type: application/json",
            "-d", payload,
            "-o", tmp_path,
        ]
        if VOICE_API_KEY:
            curl_cmd.extend(["-H", f"Authorization: Bearer {VOICE_API_KEY}"])

        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            try:
                err_body = Path(tmp_path).read_text(errors="replace")[:500]
            except Exception:
                err_body = ""
            if "quota" in err_body.lower() or "429" in err_body or "insufficient" in err_body.lower():
                log("voice_output: QUOTA EXHAUSTED — auto-disabling voice", {"returncode": result.returncode, "body": err_body[:200]})
                _write_quota_flag()
            else:
                log("voice_output: speak request failed", {"returncode": result.returncode, "stderr": stderr[:200]})
            return

        if not Path(tmp_path).exists() or Path(tmp_path).stat().st_size < 100:
            log("voice_output: empty or missing audio file", {})
            return

        ffplay_cmd = _find_ffplay()
        play_path = _audio_path_for_player(tmp_path)
        subprocess.Popen(
            [*ffplay_cmd, "-nodisp", "-autoexit", play_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        log("voice_output: ffplay or curl not found", {"error": str(e)})
    except subprocess.TimeoutExpired:
        log("voice_output: speak request timed out", {})
    except Exception as e:
        log("voice_output: speak_and_play failed", {"error": str(e)})


# ---------------------------------------------------------------------------
# Entry point (called from on_stop)
# ---------------------------------------------------------------------------


def maybe_speak(session_id: str, last_assistant_message: str) -> None:
    from hooks.config import VOICE_ENABLED, VOICE_SERVICE_URL

    if not VOICE_ENABLED:
        log("voice_output: guard: VOICE_ENABLED is false", {})
        return
    if not is_voice_enabled(session_id):
        log("voice_output: guard: voice not enabled for session", {"session_id": session_id})
        return
    if not last_assistant_message or not last_assistant_message.strip():
        log("voice_output: guard: empty message", {"session_id": session_id})
        return
    if not VOICE_SERVICE_URL:
        log("voice_output: guard: no VOICE_SERVICE_URL", {})
        return
    if _is_quota_exhausted():
        log("voice_output: guard: quota exhausted — re-enable voice to retry", {"session_id": session_id})
        return

    text = last_assistant_message.strip()

    if text.startswith("```"):
        log("voice_output: guard: code block", {"session_id": session_id})
        return
    if len(text) > 5000:
        log("voice_output: guard: too long", {"session_id": session_id, "len": len(text)})
        return

    log("voice_output: passed guards", {"session_id": session_id, "chars": len(text), "url": VOICE_SERVICE_URL})

    spoken = _summarize_with_haiku(text)
    if not spoken:
        log("voice_output: haiku returned nothing, speaking truncated", {"session_id": session_id})
        spoken = text[:300]

    _speak_and_play(spoken, VOICE_SERVICE_URL)
    log("voice_output: spoke", {"session_id": session_id, "chars": len(spoken)})
