"""Images pasted into a ledger: stored once by content hash in <slug>.media next to the ledger files."""

import hashlib
import re
import shutil
import struct
from pathlib import Path

import ledger_core as core

MAX_BYTES = 8 << 20
MAX_PER_ENTRY = 6
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
TYPES = {ext: kind for kind, ext in EXTENSIONS.items()}
ID_RE = re.compile(rf"^[0-9a-f]{{64}}\.({'|'.join(TYPES)})$")
JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class Refused(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def folder(slug):
    return core.LEDGER_DIR / f"{slug}.media"


def _jpeg_size(data):
    i = 2
    while i + 9 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in JPEG_SOF:
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return 0, 0


def _webp_size(data):
    kind = data[12:16]
    if kind == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L" and len(data) >= 25:
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X" and len(data) >= 30:
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    return 0, 0


def inspect(data):
    """(type, width, height) from the first bytes; Refused(415) for anything but PNG, JPEG, WebP or GIF."""
    size = (0, 0)
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        kind, size = "image/png", struct.unpack(">II", data[16:24])
    elif data[:3] == b"\xff\xd8\xff":
        kind, size = "image/jpeg", _jpeg_size(data)
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        kind, size = "image/gif", struct.unpack("<HH", data[6:10])
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, size = "image/webp", _webp_size(data)
    else:
        raise Refused(415, "only PNG, JPEG, WebP or GIF images are accepted")
    if not all(size):
        raise Refused(415, "the image size is unreadable")
    return kind, *size


def write_file(path: Path, data: bytes) -> None:
    with core.LOCK:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        path.touch()


def store(slug, data):
    if len(data) > MAX_BYTES:
        raise Refused(413, f"an image may be at most {MAX_BYTES >> 20} MB")
    kind, width, height = inspect(data)
    media_id = f"{hashlib.sha256(data).hexdigest()}.{EXTENSIONS[kind]}"
    path = folder(slug) / media_id
    write_file(path, data)
    return {"id": media_id, "type": kind, "size": len(data), "width": width, "height": height}


def path_of(slug, media_id):
    if not isinstance(media_id, str) or not ID_RE.match(media_id):
        raise ValueError("not a media id")
    path = folder(slug) / media_id
    if not path.is_file():
        raise ValueError(f"no stored image {media_id}")
    return path


def entry(slug, media_id):
    data = path_of(slug, media_id).read_bytes()
    kind, width, height = inspect(data)
    return {"id": media_id, "type": kind, "size": len(data), "width": width, "height": height}


def check(attachments):
    if not isinstance(attachments, list) or not 1 <= len(attachments) <= MAX_PER_ENTRY:
        raise ValueError(f"attachments must list 1 to {MAX_PER_ENTRY} images")
    for att in attachments:
        if not isinstance(att, dict) or not ID_RE.match(str(att.get("id"))):
            raise ValueError("each attachment needs a media id")


def resolve(slug, ops):
    """Each attachment rebuilt from the stored file, so the JSON holds only what the server measured."""
    for op in ops:
        if "attachments" in op:
            op["attachments"] = [entry(slug, att["id"]) for att in op["attachments"]]
    return ops


def attach_paths(slug: str, doc: dict, events: list[dict]) -> None:
    for event in events:
        target = event.get("target", "")
        thread = core.get_thread(doc, "chat" if target == "chat" else f"{target}/comments")
        item = next((item for item in thread or [] if item["id"] == event.get("id")), None)
        if item and item.get("attachments"):
            event["image_paths"] = [str(path_of(slug, att["id"]).resolve()) for att in item["attachments"]]


def purge(slug):
    if not isinstance(slug, str) or not core.SLUG_RE.match(slug):
        raise ValueError(f"refusing to purge media for {slug!r}: not a ledger slug")
    shutil.rmtree(folder(slug), ignore_errors=True)


def sweep(slug: str, used: set[str], now: int) -> None:
    # Uploads and their entry writes arrive in separate requests.
    cutoff = now / 1000 - 3600
    for path in folder(slug).glob("*"):
        if path.is_file() and path.name not in used and path.stat().st_mtime < cutoff:
            path.unlink()
