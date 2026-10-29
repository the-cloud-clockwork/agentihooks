import json
import urllib.request
from unittest.mock import patch

from scripts.swarm_ledger import ledger
from tests.swarm_ledger.test_media import Endpoint, core, media, png, server


class AgentEndpoint(Endpoint):
    def agent_upload(self, name, token=True, origin=None):
        headers = {"Host": f"127.0.0.1:{self.port}", "X-Ledger-Agent": name}
        if token:
            headers["X-Ledger-Token"] = self.token
        if origin:
            headers["Origin"] = origin
        from tests.swarm_ledger.test_media import server

        headers["Host"] = f"127.0.0.1:{server.PORT}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/media/via-media", data=png(1280, 720), headers=headers
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_joined_agent_can_upload_and_comment_with_images(self):
        self.put([{"op": "join", "id": "j", "by": "image-engineer"}])
        code, attachment = self.agent_upload("image-engineer")
        assert code == 200
        self.put([{"op": "add", "thread": "notes", "id": "slide-note", "text": "Reference slides"}])
        thread = "notes/slide-note/comments"
        op = {
            "op": "add",
            "id": "agent-slide",
            "by": "image-engineer",
            "thread": thread,
            "text": "The slide is ready to read.",
            "attachments": [attachment],
        }
        code, _, body = self.put([op])
        assert code == 200
        state = server.repository.get_document("via-media")
        comment = state["notes"][0]["comments"][-1]
        assert comment["by"] == "image-engineer"
        assert comment["attachments"] == [attachment]
        op.update(id="same-slide", text="The slide is ready to review.")
        self.put([op])
        state = server.repository.get_document("via-media")
        assert len([c for c in state["notes"][0]["comments"] if c["by"] == "image-engineer"]) == 1
        assert state["notes"][0]["comments"][-1]["attachments"] == [attachment]

    def test_agent_upload_requires_join_token_and_no_forged_origin(self):
        assert self.agent_upload("unknown-engineer")[0] == 403
        self.put([{"op": "join", "id": "j-safe", "by": "safe-engineer"}])
        assert self.agent_upload("safe-engineer", token=False)[0] == 403
        assert self.agent_upload("safe-engineer", origin="http://evil.example")[0] == 403
        self.put([{"op": "leave", "id": "left", "by": "safe-engineer"}])
        assert self.agent_upload("safe-engineer")[0] == 403


def test_comment_command_uploads_repeatable_images_and_keeps_author(tmp_path, capsys):
    slide = tmp_path / "slide.png"
    slide.write_bytes(png())
    other = tmp_path / "other.png"
    other.write_bytes(png(2, 2))
    args = ledger.build_parser().parse_args(
        [
            "--slug",
            "shots",
            "--as",
            "image-engineer",
            "comment",
            "tasks/t1",
            "The slides are ready.",
            "--image",
            str(slide),
            "--image",
            str(other),
        ]
    )
    attachments = [{"id": "a" * 64 + ".png"}, {"id": "b" * 64 + ".png"}]
    with (
        patch.object(ledger, "upload_image", side_effect=attachments) as upload,
        patch.object(ledger, "call", return_value={}) as call,
    ):
        ledger.cmd_comment(args)
    assert upload.call_args_list[0].args == ("shots", "image-engineer", str(slide))
    assert upload.call_args_list[1].args == ("shots", "image-engineer", str(other))
    op = call.call_args.args[1][0]
    assert op["attachments"] == attachments
    assert op["by"] == "image-engineer"
    assert json.loads(capsys.readouterr().out) == {"posted": True}


def test_upload_helper_sends_bytes_token_and_joined_agent(tmp_path):
    image = tmp_path / "slide.png"
    image.write_bytes(png())
    attachment = {"id": "a" * 64 + ".png"}
    with (
        patch.object(ledger.repository, "token", return_value="test-token"),
        patch.object(ledger.urllib.request, "urlopen") as opened,
    ):
        opened.return_value.__enter__.return_value.read.return_value = json.dumps(attachment).encode()
        assert ledger.upload_image("shots", "image-engineer", str(image)) == attachment
    req = opened.call_args.args[0]
    assert req.full_url.endswith("/api/v1/ledgers/shots/uploads/media")
    assert req.data == image.read_bytes()
    assert req.get_header("X-ledger-agent") == "image-engineer"
    assert req.get_header("X-ledger-token") == "test-token"


def test_unjoined_author_cannot_attach_images():
    from tests.swarm_ledger.test_bin import make_ledger

    make_ledger("unjoined-images")
    att = media.store("unjoined-images", png())
    op = {
        "op": "add",
        "id": "c",
        "by": "unknown",
        "thread": "chat",
        "text": "The slide is ready.",
        "attachments": [att],
    }
    core.check_op(op)
    assert core.sync("unjoined-images", ops=[op])[1] == ["c"]
