"""Voice output speaks through the voice service when one is set, otherwise through ElevenLabs with the tell-me skill's variables."""

import email.message
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.context import voice_output


class _Response(io.BytesIO):
    status = 200

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Capture the ElevenLabs request instead of reaching the network."""
    urlopen = Mock(return_value=_Response(b"OggS-clip"))
    monkeypatch.setattr(voice_output.urllib.request, "urlopen", urlopen)
    for name in ("ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL", "ELEVENLABS_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key-x")
    return urlopen


def test_elevenlabs_request_uses_the_tell_me_defaults(opened: Mock, tmp_path: Path) -> None:
    """Without overrides the request carries the stock voice, model and Opus format, and the clip lands at the path."""
    clip = tmp_path / "clip.ogg"

    assert voice_output._fetch_elevenlabs("Hola Miia", str(clip)) == "200"

    request = opened.call_args.args[0]
    assert (
        request.full_url
        == "https://api.elevenlabs.io/v1/text-to-speech/21m00Tcm4TlvDq8ikWAM?output_format=opus_48000_64"
    )
    assert request.get_method() == "POST"
    assert request.get_header("Xi-api-key") == "key-x"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"text": "Hola Miia", "model_id": "eleven_flash_v2_5"}
    assert opened.call_args.kwargs == {"timeout": 30}
    assert clip.read_bytes() == b"OggS-clip"


def test_elevenlabs_request_honours_the_overrides(
    opened: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The voice, model and host come from the same variables the tell-me skill reads."""
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice-y")
    monkeypatch.setenv("ELEVENLABS_MODEL", "model-z")
    monkeypatch.setenv("ELEVENLABS_API_BASE", "https://tts.example")

    voice_output._fetch_elevenlabs("Hi", str(tmp_path / "clip.ogg"))

    request = opened.call_args.args[0]
    assert request.full_url == "https://tts.example/v1/text-to-speech/voice-y?output_format=opus_48000_64"
    assert json.loads(request.data)["model_id"] == "model-z"


def test_elevenlabs_request_without_a_key_sends_an_empty_key(
    opened: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the key unset the header is present and empty, so ElevenLabs answers with a refusal the quota check reads."""
    monkeypatch.delenv("ELEVENLABS_API_KEY")

    voice_output._fetch_elevenlabs("Hi", str(tmp_path / "clip.ogg"))

    assert opened.call_args.args[0].get_header("Xi-api-key") == ""


def test_speak_hands_the_text_key_and_clip_path_to_the_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetch receives the spoken text, the service, the service key and a fresh Ogg clip path."""
    from hooks import config

    fetch = Mock(return_value="500")
    monkeypatch.setattr(config, "VOICE_API_KEY", "key-v")
    monkeypatch.setattr(voice_output, "_fetch_audio", fetch)

    voice_output._speak_and_play("Hola", "http://voice")

    text, url, key, path = fetch.call_args.args
    assert (text, url, key) == ("Hola", "http://voice", "key-v")
    assert path.endswith(".ogg")
    assert Path(path).exists()


def test_elevenlabs_error_keeps_its_body_for_the_quota_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A refused request returns its status and leaves the error body where the caller reads it."""
    refusal = urllib.error.HTTPError(
        "https://tts", 401, "Unauthorized", email.message.Message(), io.BytesIO(b'{"detail":"quota_exceeded"}')
    )
    monkeypatch.setattr(voice_output.urllib.request, "urlopen", Mock(side_effect=refusal))
    clip = tmp_path / "clip.ogg"

    assert voice_output._fetch_elevenlabs("Hi", str(clip)) == "401"
    assert clip.read_bytes() == b'{"detail":"quota_exceeded"}'


@pytest.mark.parametrize(("api_key", "bearer"), [("secret-v", ["-H", "Authorization: Bearer secret-v"]), ("", [])])
def test_voice_service_request_is_the_curl_call(
    monkeypatch: pytest.MonkeyPatch, api_key: str, bearer: list[str]
) -> None:
    """The voice service keeps its curl request, with the bearer header only when a key is set."""
    run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=" 200\n", stderr=""))
    monkeypatch.setattr(voice_output.subprocess, "run", run)

    assert voice_output._fetch_voice_service("Hi", "http://voice", api_key, "/tmp/c.ogg") == "200"

    assert run.call_args.args[0] == [
        "curl",
        "-s",
        "-X",
        "POST",
        "http://voice/speak",
        "-H",
        "Content-Type: application/json",
        "-d",
        '{"text": "Hi", "store": false}',
        "-o",
        "/tmp/c.ogg",
        "-w",
        "%{http_code}",
        *bearer,
    ]
    assert run.call_args.kwargs == {"capture_output": True, "text": True, "timeout": 30}


@pytest.mark.parametrize(("url", "service", "elevenlabs"), [("http://voice", 1, 0), ("", 0, 1)])
def test_speak_picks_the_service_when_set_else_elevenlabs(
    monkeypatch: pytest.MonkeyPatch, url: str, service: int, elevenlabs: int
) -> None:
    """A configured voice service wins; without one the clip comes from ElevenLabs."""
    from hooks import config

    via_service = Mock(return_value="500")
    via_elevenlabs = Mock(return_value="500")
    monkeypatch.setattr(config, "VOICE_API_KEY", "")
    monkeypatch.setattr(voice_output, "_fetch_voice_service", via_service)
    monkeypatch.setattr(voice_output, "_fetch_elevenlabs", via_elevenlabs)

    assert voice_output._speak_and_play("Hi", url) is False
    assert (via_service.call_count, via_elevenlabs.call_count) == (service, elevenlabs)


@pytest.mark.parametrize(("url", "expected"), [("http://voice", "service"), ("", "elevenlabs")])
def test_fetch_audio_hands_its_arguments_to_the_chosen_backend(
    monkeypatch: pytest.MonkeyPatch, url: str, expected: str
) -> None:
    """The chosen backend gets the text, the service details and the clip path, and its status comes back."""
    monkeypatch.setattr(voice_output, "_fetch_voice_service", lambda *args: ("service", args))
    monkeypatch.setattr(voice_output, "_fetch_elevenlabs", lambda *args: ("elevenlabs", args))

    backend, args = voice_output._fetch_audio("Hi", url, "key-v", "/tmp/c.ogg")

    assert backend == expected
    assert args == (("Hi", url, "key-v", "/tmp/c.ogg") if url else ("Hi", "/tmp/c.ogg"))


@pytest.mark.parametrize(("url", "key", "spoken"), [("", "key-x", 1), ("http://voice", "", 1), ("", "", 0)])
def test_maybe_speak_needs_the_service_or_an_elevenlabs_key(
    monkeypatch: pytest.MonkeyPatch, url: str, key: str, spoken: int
) -> None:
    """An ElevenLabs key alone is enough to speak; with neither backend nothing is started."""
    from hooks import _async, config

    fork = Mock()
    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "VOICE_SERVICE_URL", url)
    monkeypatch.setenv("ELEVENLABS_API_KEY", key)
    monkeypatch.setattr(voice_output, "is_voice_enabled", lambda _session: True)
    monkeypatch.setattr(voice_output, "_is_quota_exhausted", lambda: False)
    monkeypatch.setattr(voice_output, "_check_cooldown", lambda: True)
    monkeypatch.setattr(_async, "fork_and_call", fork)

    voice_output.maybe_speak("session", "All done.")

    assert fork.call_count == spoken


def test_windows_playback_is_stopped_with_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the running ffplay is ended with taskkill, the only stop that exists there."""
    run = Mock()
    monkeypatch.setattr(voice_output.subprocess, "run", run)
    monkeypatch.setattr(voice_output, "_is_wsl2", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")

    voice_output._kill_existing_playback()

    run.assert_called_once_with(["taskkill", "/IM", "ffplay.exe", "/F"], capture_output=True, timeout=5)
