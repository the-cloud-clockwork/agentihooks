import json
import urllib.error
import urllib.request
from unittest.mock import patch

import ledger_artifacts as artifacts  # noqa: E402
import pytest

from scripts.swarm import prompt
from scripts.swarm_ledger import ledger
from tests.swarm_ledger.test_media import Endpoint, core, media, png, server

MARKDOWN = b"# Handoff template\n\n| Field | Use |\n| --- | --- |\n| Done | what landed |\n\n```\nagentihooks swarm done\n```\n"
JSON_DOC = b'{"proposal": {"sections": ["Done", "Stopped at"], "version": 2}}'
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="40" height="20" '
    b'onload="parent.pwned=1"><script>parent.pwned=1</script><rect width="40" height="20" fill="red" '
    b'onclick="parent.pwned=1"/><a xlink:href="javascript:parent.pwned=1"><text>x</text></a>'
    b'<image href="missing.png" onerror="parent.pwned=1"/>'
    b'<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject></svg>'
)


class TestStore:
    def test_markdown_json_and_svg_are_stored_once_by_content_hash(self):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        md = artifacts.store("arts", "proposal.md", MARKDOWN)
        assert md == artifacts.store("arts", "again.md", MARKDOWN)
        assert md["id"].endswith(".md") and md["type"] == "text/markdown" and md["size"] == len(MARKDOWN)
        assert artifacts.path_of("arts", md["id"]).read_bytes() == MARKDOWN
        assert artifacts.store("arts", "data.json", JSON_DOC)["type"] == "application/json"
        assert artifacts.store("arts", "diagram.svg", SVG)["type"] == "image/svg+xml"
        shot = artifacts.store("arts", "shot.png", png(4, 3))
        assert (shot["type"], shot["width"], shot["height"]) == ("image/png", 4, 3)

    def test_svg_is_stored_without_scripts_handlers_or_script_links(self):
        stored = artifacts.path_of("arts", artifacts.store("arts", "diagram.svg", SVG)["id"]).read_text()
        for gone in ("script", "onload", "onclick", "onerror", "javascript", "missing.png", "foreignObject"):
            assert gone not in stored
        assert "<rect" in stored and "<text>x</text>" in stored

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("broken.json", b"{not json"),
            ("notes.md", b"\xff\xfe not utf8"),
            ("page.html", b"<html><script>alert(1)</script></html>"),
            ("plain.svg", b"<svg><script>x</script></svg>"),
            ("bomb.svg", b'<!DOCTYPE svg [<!ENTITY a "aa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>'),
        ],
    )
    def test_anything_else_is_refused(self, name, data):
        with pytest.raises(media.Refused) as caught:
            artifacts.store("arts", name, data)
        assert caught.value.status == 415

    def test_an_oversize_file_is_refused(self):
        with pytest.raises(media.Refused) as caught:
            artifacts.store("arts", "big.md", b"#" * (artifacts.MAX_BYTES + 1))
        assert caught.value.status == 413


class ArtifactEndpoint(Endpoint):
    def publish(self, name, filename, data, token=True):
        headers = {"Host": f"127.0.0.1:{server.PORT}", "X-Ledger-Agent": name, "X-Artifact-Name": filename}
        if token:
            headers["X-Ledger-Token"] = self.token
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/artifacts/via-media", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_an_agent_publishes_markdown_json_and_svg_on_a_task(self):
        self.put([{"op": "join", "id": "j-art", "by": "art-engineer"}])
        task = {
            "op": "task_add",
            "id": "t-art",
            "by": "art-engineer",
            "task": "av1",
            "title": "Artifacts",
            "lane": "eng",
        }
        core.sync("via-media", ops=[task])
        published = []
        for filename, data, title in (
            ("proposal.md", MARKDOWN, "Handoff template proposal"),
            ("shape.json", JSON_DOC, "Proposal shape"),
            ("diagram.svg", SVG, "Handoff flow diagram"),
        ):
            code, file = self.publish("art-engineer", filename, data)
            assert code == 200, file
            op = {
                "op": "artifact_add",
                "id": f"a-{filename}",
                "by": "art-engineer",
                "task": "av1",
                "title": title,
                "file": {**file, "size": 1},
            }
            code, _, body = self.put([op])
            assert code == 200
            published.append((title, file))
        state = json.loads(body)
        rows = state["artifacts"][-3:]
        assert [(r["title"], r["file"]) for r in rows] == published
        assert {(r["by"], r["task"]) for r in rows} == {("art-engineer", "av1")}
        assert all(isinstance(r["at"], int) for r in rows)
        code, headers, served = self.call(
            "GET", f"/artifacts/via-media/{published[2][1]['id']}", token=False, origin=False
        )
        assert code == 200 and b"script" not in served
        assert headers["Content-Type"] == "image/svg+xml"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'none'" in headers["Content-Security-Policy"]
        code, headers, served = self.call(
            "GET", f"/artifacts/via-media/{published[0][1]['id']}", token=False, origin=False
        )
        assert (code, served) == (200, MARKDOWN)
        assert headers["Content-Type"].startswith("text/markdown")

    def test_publishing_needs_a_joined_agent_with_the_token(self):
        assert self.publish("stranger", "proposal.md", MARKDOWN)[0] == 403
        self.put([{"op": "join", "id": "j-tok", "by": "tok-engineer"}])
        assert self.publish("tok-engineer", "proposal.md", MARKDOWN, token=False)[0] == 403

    def test_a_record_needs_a_stored_file_a_member_and_a_known_task(self):
        self.put([{"op": "join", "id": "j-rec", "by": "rec-engineer"}])
        file = self.publish("rec-engineer", "proposal.md", MARKDOWN)[1]
        ghost = {"id": "f" * 64 + ".md"}
        base = {"op": "artifact_add", "by": "rec-engineer", "task": "", "title": "Plan"}
        assert self.put([{**base, "id": "a-ghost", "file": ghost}])[0] == 400
        state = json.loads(self.put([{**base, "id": "a-anon", "by": "stranger", "file": file}])[2])
        assert "a-anon" in state["rejected"]
        state = json.loads(self.put([{**base, "id": "a-notask", "task": "nope", "file": file}])[2])
        assert "a-notask" in state["rejected"]
        state = json.loads(self.put([{**base, "id": "a-free", "file": file}])[2])
        assert state["artifacts"][-1]["id"] == "a-free"


def test_artifact_op_shape_is_checked():
    good = {
        "op": "artifact_add",
        "id": "a",
        "by": "eng",
        "task": "t1",
        "title": "Plan",
        "file": {"id": "a" * 64 + ".md"},
    }
    core.check_op(good)
    for bad in (
        {**good, "by": "operator"},
        {**good, "title": ""},
        {**good, "file": {"id": "../x.md"}},
        {**good, "extra": 1},
    ):
        with pytest.raises(ValueError):
            core.check_op(bad)


def test_artifact_command_uploads_the_file_and_records_it_on_the_task(tmp_path, capsys, monkeypatch):
    doc = tmp_path / "proposal.md"
    doc.write_bytes(MARKDOWN)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "av1")
    args = ledger.build_parser().parse_args(
        ["--slug", "shots", "--as", "art-engineer", "artifact", str(doc), "Handoff template proposal"]
    )
    file = {"id": "a" * 64 + ".md", "type": "text/markdown", "size": len(MARKDOWN)}
    with (
        patch.object(ledger, "upload_artifact", return_value=file) as upload,
        patch.object(ledger, "call", return_value={}) as call,
    ):
        ledger.cmd_artifact(args)
    assert upload.call_args.args == ("shots", "art-engineer", str(doc))
    op = call.call_args.args[1][0]
    assert {k: op[k] for k in ("op", "by", "task", "title", "file")} == {
        "op": "artifact_add",
        "by": "art-engineer",
        "task": "av1",
        "title": "Handoff template proposal",
        "file": file,
    }
    assert json.loads(capsys.readouterr().out) == {"published": True}


def test_upload_artifact_sends_bytes_name_token_and_agent(tmp_path):
    doc = tmp_path / "proposal.md"
    doc.write_bytes(MARKDOWN)
    with (
        patch.object(ledger.core, "paths", return_value=(tmp_path / "page.html", None)),
        patch.object(ledger.core, "read_token", return_value="test-token"),
        patch.object(ledger.urllib.request, "urlopen") as opened,
    ):
        (tmp_path / "page.html").write_text("page")
        opened.return_value.__enter__.return_value.read.return_value = b'{"id": "x"}'
        assert ledger.upload_artifact("shots", "art-engineer", str(doc)) == {"id": "x"}
    req = opened.call_args.args[0]
    assert req.full_url.endswith("/api/artifacts/shots")
    assert req.data == MARKDOWN
    assert req.get_header("X-artifact-name") == "proposal.md"
    assert req.get_header("X-ledger-agent") == "art-engineer"
    assert req.get_header("X-ledger-token") == "test-token"


def test_swarm_prompt_tells_agents_to_publish_what_needs_operator_review():
    task = {"id": "av1", "title": "Artifacts", "phase": "p7", "lane": "eng"}
    text = prompt.build("rig", "/repo", "eng", "engineer@1", task)
    assert "ledger --slug rig --as engineer@1 artifact <file>" in text
    assert "operator review" in text
