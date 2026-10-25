import http.client
import json
import struct
import sys
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_bin  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_media as media  # noqa: E402
import ledger_server as server  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.test_bin import DAY_MS, make_ledger  # noqa: E402


def png(width=3, height=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


GIF = b"GIF89a" + struct.pack("<HH", 5, 4) + b"\x00\x00\x00;"
JPEG = (
    b"\xff\xd8\xff\xe0"
    + struct.pack(">H", 16)
    + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\xff\xc0"
    + struct.pack(">HBHHB", 11, 8, 7, 9, 1)
    + b"\x01\x11\x00\xff\xd9"
)
WEBP_LOSSLESS = b"RIFF" + struct.pack("<I", 26) + b"WEBPVP8L" + struct.pack("<I", 5) + b"\x2f"
WEBP_LOSSLESS += struct.pack("<I", (10 - 1) | ((6 - 1) << 14)) + b"\x00\x00"


class Detect(unittest.TestCase):
    def test_each_allowed_type_is_known_by_its_first_bytes_with_its_size(self):
        self.assertEqual(media.inspect(png(3, 2)), ("image/png", 3, 2))
        self.assertEqual(media.inspect(GIF), ("image/gif", 5, 4))
        self.assertEqual(media.inspect(JPEG), ("image/jpeg", 9, 7))
        self.assertEqual(media.inspect(WEBP_LOSSLESS), ("image/webp", 10, 6))

    def test_a_non_image_is_refused_whatever_it_claims_to_be(self):
        for data in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"%PDF-1.7", b"hello", b""):
            with self.assertRaises(media.Refused) as caught:
                media.inspect(data)
            self.assertEqual(caught.exception.status, 415)


class Store(unittest.TestCase):
    def setUp(self):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)

    def test_an_image_is_stored_once_by_content_hash_next_to_the_ledger(self):
        data = png(4, 4)
        first = media.store("shots", data)
        again = media.store("shots", data)
        self.assertEqual(first, again)
        self.assertEqual(set(first), {"id", "type", "size", "width", "height"})
        self.assertEqual(
            (first["type"], first["size"], first["width"], first["height"]), ("image/png", len(data), 4, 4)
        )
        folder = core.LEDGER_DIR / "shots.media"
        self.assertEqual([p.name for p in folder.iterdir()], [first["id"]])
        self.assertEqual((folder / first["id"]).read_bytes(), data)

    def test_an_oversize_image_is_refused(self):
        big = png() + b"\x00" * media.MAX_BYTES
        with self.assertRaises(media.Refused) as caught:
            media.store("shots", big)
        self.assertEqual(caught.exception.status, 413)

    def test_an_unknown_or_malformed_id_does_not_resolve(self):
        stored = media.store("shots", GIF)
        self.assertEqual(media.entry("shots", stored["id"]), stored)
        for bad in ("0" * 64 + ".png", "../shots.json", stored["id"] + "/x", 7):
            with self.assertRaises(ValueError):
                media.entry("shots", bad)


class Lifecycle(unittest.TestCase):
    def test_the_bin_keeps_media_and_the_purge_deletes_it(self):
        make_ledger("pictured")
        media.store("pictured", png())
        folder = core.LEDGER_DIR / "pictured.media"
        ledger_bin.delete("pictured", now=0)
        self.assertEqual(ledger_bin.purge_expired(now=29 * DAY_MS), [])
        self.assertTrue(any(folder.iterdir()))
        self.assertEqual(ledger_bin.purge_expired(now=30 * DAY_MS + 1), ["pictured"])
        self.assertFalse(folder.exists())


class Endpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        html_path, _ = make_ledger("via-media")
        cls.token = legacy_page.stored_token(html_path)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, args=(0.01,), daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method, path, data=None, token=True, origin=True, ctype="application/octet-stream"):
        headers = {"Host": f"127.0.0.1:{server.PORT}", "Content-Type": ctype}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{server.PORT}"
        if token:
            headers["X-Ledger-Token"] = self.token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def upload(self, data, **kw):
        return self.call("POST", "/api/media/via-media", data, **kw)

    def test_an_upload_is_stored_and_served_back_with_its_type_and_no_sniffing(self):
        data = png(6, 5)
        code, _, body = self.upload(data)
        self.assertEqual(code, 200)
        entry = json.loads(body)
        self.assertEqual(
            (entry["type"], entry["width"], entry["height"], entry["size"]), ("image/png", 6, 5, len(data))
        )
        code, headers, served = self.call("GET", f"/media/via-media/{entry['id']}", token=False, origin=False)
        self.assertEqual((code, served), (200, data))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_a_non_image_upload_is_refused(self):
        code, _, body = self.upload(b"<html><script>alert(1)</script>", ctype="image/png")
        self.assertEqual(code, 415)
        self.assertIn(b"PNG, JPEG, WebP or GIF", body)

    def test_an_oversize_upload_is_refused_before_its_body_is_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/media/via-media", skip_host=True)
        for name, value in (
            ("Host", f"127.0.0.1:{server.PORT}"),
            ("Origin", f"http://127.0.0.1:{server.PORT}"),
            ("X-Ledger-Token", self.token),
            ("Content-Length", str(media.MAX_BYTES + 1)),
        ):
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_an_upload_without_the_token_or_from_a_forged_origin_is_refused(self):
        self.assertEqual(self.upload(png(), token=False)[0], 403)
        self.assertEqual(self.upload(png(), origin=False)[0], 403)

    def test_a_missing_or_malformed_media_path_is_not_found(self):
        self.assertEqual(self.call("GET", "/media/via-media/" + "0" * 64 + ".png", token=False)[0], 404)
        self.assertEqual(self.call("GET", "/media/via-media/..%2Fvia-media.json", token=False)[0], 404)

    def put(self, ops):
        body = json.dumps({"changes": [], "ops": ops}).encode()
        return self.call("PUT", "/api/via-media", body, ctype="application/json")

    def test_a_chat_line_and_a_comment_keep_attachment_entries_never_bytes(self):
        entry = json.loads(self.upload(GIF)[2])
        forged = {**entry, "width": 9999, "type": "text/html"}
        code, _, body = self.put([{"op": "add", "thread": "chat", "id": "m-pic", "text": "", "attachments": [forged]}])
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["rejected"], [])
        state = server.repository.get_document("via-media")
        line = next(e for e in state["chat"] if e["id"] == "m-pic")
        self.assertEqual(line["attachments"], [entry])
        stored = json.dumps(server.repository.export_document("via-media"))
        self.assertNotIn("GIF89a", stored)

    def test_an_attachment_the_server_does_not_hold_is_refused(self):
        ghost = {"id": "f" * 64 + ".png", "type": "image/png", "size": 1, "width": 1, "height": 1}
        code, _, _ = self.put([{"op": "add", "thread": "chat", "id": "m-ghost", "text": "x", "attachments": [ghost]}])
        self.assertEqual(code, 400)


class Ops(unittest.TestCase):
    def test_attachments_ride_only_on_an_operator_add_to_chat_or_comments(self):
        att = [{"id": "a" * 64 + ".png"}]
        core.check_op({"op": "add", "thread": "chat", "id": "m", "text": "", "attachments": att})
        core.check_op({"op": "add", "thread": "tasks/t1/comments", "id": "c", "text": "x", "attachments": att})
        for bad in (
            {"op": "add", "thread": "notes", "id": "n", "text": "x", "attachments": att},
            {"op": "edit", "thread": "chat", "id": "m", "text": "x", "attachments": att},
            {"op": "add", "thread": "chat", "id": "m", "text": "x", "attachments": "nope"},
            {"op": "add", "thread": "chat", "id": "m", "text": "x", "attachments": att * (media.MAX_PER_ENTRY + 1)},
        ):
            with self.assertRaises(ValueError):
                core.check_op(bad)
