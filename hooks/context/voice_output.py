"""Voice output — spoken summaries of Claude responses via Anton Voice Service.

Toggle: operator says "enable voice" / "disable voice" in chat.
Pipeline: Stop hook → fork → Haiku summarizes → POST /speak → ffplay plays OGG.
Session-scoped: persists until session end or explicit disable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log

VOICE_TYPE = "voice_enabled"
_FLAG_DIR = Path.home() / ".agentihooks" / "voice_flags"
_QUOTA_FLAG = _FLAG_DIR / "quota_exhausted"
_LAST_SPOKE_FLAG = _FLAG_DIR / "last_spoke_ts"
_COOLDOWN_SECONDS = 10

_RE_ENABLE = re.compile(r"\b(enable|turn\s+on|activate)\s+voice\b", re.IGNORECASE)
_RE_DISABLE = re.compile(r"\b(disable|turn\s+off|deactivate)\s+voice\b", re.IGNORECASE)

_DEFAULT_SUMMARIZER_PREFIX = (
    "Distill this into exactly ONE short sentence for a voice briefing. "
    "Max 20 words. No lists. No bullet points. No filenames. No commit hashes. "
    "Just the key takeaway a human needs to hear in 5 seconds: "
)


def _get_summarizer_prefix() -> str:
    return os.getenv("VOICE_SUMMARIZER_PREFIX", _DEFAULT_SUMMARIZER_PREFIX)


# ---------------------------------------------------------------------------
# Quota management (global, auto-expires after 1 hour)
# ---------------------------------------------------------------------------


def _write_quota_flag() -> None:
    _FLAG_DIR.mkdir(parents=True, exist_ok=True)
    _QUOTA_FLAG.write_text(str(int(time.time())))


def _is_quota_exhausted() -> bool:
    if not _QUOTA_FLAG.exists():
        return False
    try:
        ts = int(_QUOTA_FLAG.read_text().strip())
        if time.time() - ts > 3600:
            _QUOTA_FLAG.unlink(missing_ok=True)
            return False
        return True
    except (ValueError, OSError):
        return False


def check_quota_banner(session_id: str) -> str | None:
    if _is_quota_exhausted() and is_voice_enabled(session_id):
        return (
            "Voice output UNAVAILABLE — voice service quota exhausted. "
            "Responses will not be spoken until credits are topped up. "
            "Say 'enable voice' after replenishing to retry."
        )
    return None


# ---------------------------------------------------------------------------
# Rate limiting (max 1 speak per COOLDOWN_SECONDS)
# ---------------------------------------------------------------------------


def _check_cooldown() -> bool:
    try:
        if not _LAST_SPOKE_FLAG.exists():
            return True
        ts = float(_LAST_SPOKE_FLAG.read_text().strip())
        return (time.time() - ts) >= _COOLDOWN_SECONDS
    except (ValueError, OSError):
        return True


def _record_spoke() -> None:
    try:
        _FLAG_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_SPOKE_FLAG.write_text(str(time.time()))
    except OSError:
        pass


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


def cleanup_stale_flags(max_age_seconds: int = 86400) -> int:
    """Remove voice flags older than max_age_seconds. Returns count removed."""
    removed = 0
    try:
        if not _FLAG_DIR.exists():
            return 0
        now = time.time()
        for f in _FLAG_DIR.glob("*.voice"):
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


# ---------------------------------------------------------------------------
# Haiku summarizer (CLI, no --bare — uses existing OAuth auth)
# ---------------------------------------------------------------------------


def _summarize_with_haiku(text: str) -> str | None:
    clamped = text[:1500] if len(text) > 1500 else text
    # Strip noise that shouldn't be spoken
    clamped = re.sub(r"\b[0-9a-f]{7,40}\b", "", clamped)  # git hashes
    clamped = re.sub(r"```[\s\S]*?```", "", clamped)  # code blocks
    clamped = re.sub(r"`[^`]+`", "", clamped)  # inline code
    clamped = re.sub(r"https?://\S+", "", clamped)  # URLs
    clamped = re.sub(r"\s+", " ", clamped).strip()  # collapse whitespace
    prefix = _get_summarizer_prefix()
    prompt = f"{prefix}{clamped}"
    try:
        result = subprocess.run(
            ["claude", prompt, "-p", "--model", "haiku"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            out = result.stdout.strip()
            if len(out) > 150:
                for end in [". ", "! ", "? "]:
                    idx = out[:150].rfind(end)
                    if idx > 0:
                        out = out[:idx + 1]
                        break
                else:
                    out = out[:150]
            return out
        log("voice_output: haiku returned empty", {"returncode": result.returncode, "stderr": (result.stderr or "")[:200]})
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


_wsl2_cache: bool | None = None


def _is_wsl2() -> bool:
    global _wsl2_cache
    if _wsl2_cache is None:
        try:
            _wsl2_cache = "microsoft" in Path("/proc/version").read_text().lower()
        except Exception:
            _wsl2_cache = False
    return _wsl2_cache


def _is_macos() -> bool:
    import platform
    return platform.system() == "Darwin"


def _find_player() -> list[str]:
    """Find audio player — WSL2 uses Windows ffplay.exe, macOS uses afplay, Linux uses ffplay."""
    if _is_wsl2():
        try:
            result = subprocess.run(
                ["bash", "-c", "command -v ffplay.exe 2>/dev/null || /mnt/c/Windows/System32/where.exe ffplay 2>/dev/null | head -1"],
                capture_output=True, text=True, timeout=5,
            )
            path = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            if path:
                wsl_path = path if path.startswith("/") else f"/mnt/{path[0].lower()}{path[2:].replace(chr(92), '/')}"
                return [wsl_path]
        except Exception:
            pass
        for candidate in [
            "/mnt/c/Tools/ffmpeg-7.0/bin/ffplay.exe",
            "/mnt/c/ProgramData/chocolatey/bin/ffplay.exe",
        ]:
            if Path(candidate).exists():
                return [candidate]
    if _is_macos():
        return ["afplay"]
    return ["ffplay"]


def _audio_path_for_player(path: str) -> str:
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


def _convert_for_macos(ogg_path: str) -> str | None:
    """Convert OGG to WAV for macOS afplay."""
    wav_path = ogg_path.replace(".ogg", ".wav")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_path, wav_path],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0 and Path(wav_path).exists():
            return wav_path
    except Exception:
        pass
    return None


def _kill_existing_playback() -> None:
    """Kill any running ffplay/afplay to prevent overlapping audio."""
    try:
        if _is_wsl2():
            subprocess.run(
                ["taskkill.exe", "/IM", "ffplay.exe", "/F"],
                capture_output=True, timeout=5,
            )
        elif _is_macos():
            subprocess.run(["pkill", "-x", "afplay"], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-x", "ffplay"], capture_output=True, timeout=5)
    except Exception:
        pass


def _speak_and_play(text: str, voice_service_url: str) -> bool:
    """Returns True if audio was played successfully."""
    try:
        from hooks.config import VOICE_API_KEY

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        payload = json.dumps({"text": text, "store": False})
        curl_cmd = [
            "curl", "-s",
            "-X", "POST",
            f"{voice_service_url}/speak",
            "-H", "Content-Type: application/json",
            "-d", payload,
            "-o", tmp_path,
            "-w", "%{http_code}",
        ]
        if VOICE_API_KEY:
            curl_cmd.extend(["-H", f"Authorization: Bearer {VOICE_API_KEY}"])

        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        http_code = result.stdout.strip() if result.stdout else ""

        if http_code != "200":
            try:
                err_body = Path(tmp_path).read_text(errors="replace")[:500]
            except Exception:
                err_body = ""
            if http_code == "429" or "quota" in err_body.lower() or "insufficient" in err_body.lower():
                log("voice_output: QUOTA EXHAUSTED — auto-disabling voice", {"http": http_code, "body": err_body[:200]})
                _write_quota_flag()
            else:
                log("voice_output: speak request failed", {"http": http_code, "body": err_body[:200]})
            return False

        if not Path(tmp_path).exists() or Path(tmp_path).stat().st_size < 100:
            log("voice_output: empty or missing audio file", {})
            return False

        _kill_existing_playback()

        player_cmd = _find_player()
        play_path = tmp_path

        if _is_macos() and player_cmd == ["afplay"]:
            wav = _convert_for_macos(tmp_path)
            if wav:
                play_path = wav
            else:
                log("voice_output: macOS OGG→WAV conversion failed", {})
                return False
        else:
            play_path = _audio_path_for_player(tmp_path)

        subprocess.Popen(
            [*player_cmd, "-nodisp", "-autoexit", play_path] if "afplay" not in player_cmd[0] else [*player_cmd, play_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except FileNotFoundError as e:
        log("voice_output: player or curl not found", {"error": str(e)})
        return False
    except subprocess.TimeoutExpired:
        log("voice_output: speak request timed out", {})
        return False
    except Exception as e:
        log("voice_output: speak_and_play failed", {"error": str(e)})
        return False


# ---------------------------------------------------------------------------
# Entry point (called from on_stop)
# ---------------------------------------------------------------------------


def maybe_speak(session_id: str, last_assistant_message: str) -> None:
    from hooks.config import VOICE_ENABLED, VOICE_SERVICE_URL

    if not VOICE_ENABLED:
        return
    if not is_voice_enabled(session_id):
        return
    if not last_assistant_message or not last_assistant_message.strip():
        return
    if not VOICE_SERVICE_URL:
        return
    if _is_quota_exhausted():
        return
    if not _check_cooldown():
        log("voice_output: cooldown active, skipping", {"session_id": session_id})
        return

    text = last_assistant_message.strip()

    if text.startswith("```"):
        return
    if len(text) > 5000:
        return

    pid = os.fork()
    if pid != 0:
        return

    try:
        spoken = _summarize_with_haiku(text)
        if not spoken:
            log("voice_output: haiku returned nothing, speaking truncated", {"session_id": session_id})
            spoken = text[:300]

        success = _speak_and_play(spoken, VOICE_SERVICE_URL)
        if success:
            _record_spoke()
            log("voice_output: spoke", {"session_id": session_id, "chars": len(spoken)})
    except Exception as e:
        log("voice_output: background process failed", {"error": str(e)})
    finally:
        os._exit(0)
