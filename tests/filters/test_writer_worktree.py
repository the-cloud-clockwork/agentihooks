import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

import hooks.filters.check as filter_check  # noqa: E402
import scripts.swarm_ledger.api.routes as api_routes  # noqa: E402
import scripts.swarm_ledger.ledger_comments as ledger_comments  # noqa: E402
from scripts.swarm_ledger import ledger_agent_ops, ledger_core, ledger_link  # noqa: E402
from scripts.swarm_ledger.api import client, mutations, schemas  # noqa: E402
from scripts.swarm_ledger.api.errors import APIError  # noqa: E402

FILTER = """mode: finders
finders:
  - regex: 'because accounts have quota'
    reason: explains the change
"""
TEXT = "The capacity line changed because accounts have quota"
SENT_BACK = (
    "[condition pre-ledger_write-x.filter.yaml] filter sent the text back:\n"
    '- "because accounts have quota": explains the change'
)
UNTRUSTED = "the writer's worktree is outside the operator's trusted repositories"


@pytest.fixture
def server_elsewhere(project_filters, tmp_path, monkeypatch):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.chdir(server)
    return str(project_filters.parent.parent)


@pytest.fixture
def stranger(tmp_path, monkeypatch):
    monkeypatch.setattr("hooks.config.CONDITIONS_TRUSTED_OWNERS", "")
    repo = tmp_path / "stranger"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:stranger/x.git"], cwd=repo, check=True)
    (repo / ".agentihooks" / "conditions").mkdir(parents=True)
    return str(repo)


def _comment(worktree, text=TEXT):
    return {"op": "add", "id": "c1", "thread": "phases/p1/comments", "text": text, "by": "eng", "worktree": worktree}


def _followup(worktree, text=TEXT):
    return {"op": "add_item", "id": "f1", "list": "followups", "text": text, "by": "eng", "worktree": worktree}


def _bare(op):
    return {key: value for key, value in op.items() if key != "worktree"}


class Applied(Exception):
    pass


def _services(seen):
    def resolve(slug, operations):
        seen.extend(operations)
        raise Applied

    return SimpleNamespace(
        repository=SimpleNamespace(current=lambda slug: SimpleNamespace(state={"tasks": [], "_meta": {}})),
        ledger=SimpleNamespace(core=ledger_core, authority=SimpleNamespace(refusal=lambda principal, op: None)),
        media=SimpleNamespace(resolve_media=resolve),
    )


def _payload(*ops):
    return {"operation_id": "o1", "ops": [_bare(op) for op in ops], "guards": {}}


def test_the_server_refuses_a_comment_by_the_project_filter_of_the_writers_repo_while_it_runs_elsewhere(
    server_elsewhere,
):
    with pytest.raises(APIError) as refused:
        mutations.apply(_services([]), "demo", "", _payload(_comment("")), server_elsewhere)
    assert (refused.value.status, str(refused.value)) == (
        400,
        f"Operation does not match its domain schema: {SENT_BACK}",
    )


def test_the_server_refuses_a_follow_up_written_from_a_subfolder_of_the_filtered_repo(server_elsewhere):
    inner = Path(server_elsewhere) / "src"
    inner.mkdir()
    with pytest.raises(APIError) as refused:
        mutations.apply(_services([]), "demo", "", _payload(_followup("")), str(inner))
    assert str(refused.value) == f"Operation does not match its domain schema: {SENT_BACK}"


def test_without_the_writers_folder_the_server_folder_decides_and_the_write_passes(server_elsewhere):
    seen = []
    with pytest.raises(Applied):
        mutations.apply(_services(seen), "demo", "", _payload(_comment(""), _followup("")))
    assert seen == [_bare(_comment("")), _bare(_followup(""))]


def test_a_passing_write_reaches_the_ledger_without_the_folder(server_elsewhere):
    seen = []
    plain = "The capacity line changed"
    with pytest.raises(Applied):
        mutations.apply(
            _services(seen), "demo", "", _payload(_comment("", plain), _followup("", plain)), server_elsewhere
        )
    assert seen == [_bare(_comment("", plain)), _bare(_followup("", plain))]


def test_the_server_stamps_the_header_folder_on_every_screened_write_and_drops_a_body_folder():
    claim = {"op": "claim", "id": "k1", "by": "eng", "item": "phases/p1", "worktree": "/body"}
    edit = {"op": "edit", "id": "c1", "thread": "phases/p1/comments", "text": TEXT, "by": "eng"}
    retext = {"op": "retext", "id": "r1", "item": "followups/f1", "text": TEXT, "by": "eng"}
    ops = [_comment("/body"), _followup(""), edit, retext, claim]
    assert mutations.stamped({"ops": ops}, "/w")["ops"] == [
        _comment("/w"),
        _followup("/w"),
        {**edit, "worktree": "/w"},
        {**retext, "worktree": "/w"},
        claim,
    ]
    assert mutations.stamped({"ops": ops}, "")["ops"] == [
        _bare(_comment("")),
        _bare(_followup("")),
        edit,
        retext,
        claim,
    ]
    assert mutations.stamped({"ops": "bad"}, "/w") == {"ops": "bad"}
    assert mutations.stamped({"ops": [5]}, "/w") == {"ops": [5]}


def test_the_server_reads_the_quoted_folder_header():
    assert mutations.writer({"X-Ledger-Worktree": "/home/a%20b/repo"}) == "/home/a b/repo"
    assert mutations.writer({}) == ""


def test_comment_edits_and_follow_up_retexts_screen_with_the_writers_folder(server_elsewhere):
    edit = {"op": "edit", "id": "c1", "thread": "phases/p1/comments", "text": TEXT, "by": "eng"}
    retext = {"op": "retext", "id": "r1", "item": "followups/f1", "text": TEXT, "by": "eng"}
    for check, op in ((ledger_core.check_op, edit), (ledger_agent_ops.check, retext)):
        assert check(dict(op)) is None
        with pytest.raises(ValueError) as refused:
            check({**op, "worktree": server_elsewhere})
        assert str(refused.value) == SENT_BACK
    question = {**retext, "item": "questions/q1", "worktree": server_elsewhere}
    assert ledger_agent_ops.check(question) is None


def test_a_comment_or_follow_up_from_an_untrusted_repository_is_refused(server_elsewhere, stranger):
    for check, op in ((ledger_core.check_op, _comment), (ledger_agent_ops.check, _followup)):
        with pytest.raises(ValueError) as refused:
            check(op(stranger, "A plain line"))
        assert str(refused.value) == UNTRUSTED


def test_with_conditions_off_an_untrusted_worktree_passes(server_elsewhere, stranger, monkeypatch):
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", False)
    assert ledger_core.check_op(_comment(stranger)) is None
    assert ledger_agent_ops.check(_followup(stranger)) is None


def test_an_untrusted_repository_without_project_conditions_passes(server_elsewhere, stranger):
    (Path(stranger) / ".agentihooks" / "conditions").rmdir()
    assert ledger_core.check_op(_comment(stranger)) is None
    assert ledger_agent_ops.check(_followup(stranger)) is None


@pytest.mark.parametrize("worktree", ["relative/path", "", 7])
def test_a_worktree_must_be_an_absolute_path(server_elsewhere, worktree):
    for check, op in ((ledger_core.check_op, _comment), (ledger_agent_ops.check, _followup)):
        with pytest.raises(ValueError) as refused:
            check(op(worktree, "A plain line"))
        assert str(refused.value) == "worktree must be the writer's absolute folder path"


def test_a_worktree_on_chat_or_a_question_is_ignored(server_elsewhere):
    chat = {**_comment(server_elsewhere), "thread": "chat"}
    question = {**_followup(server_elsewhere), "list": "questions"}
    assert ledger_core.check_op(chat) is None
    assert ledger_agent_ops.check(question) is None


def test_only_an_agent_comment_reads_its_worktree(server_elsewhere):
    chat = {**_comment("relative/path"), "thread": "chat"}
    operator_comment = {key: value for key, value in _comment("relative/path").items() if key != "by"}
    assert ledger_core.check_op(chat) is None
    assert ledger_core.check_op(operator_comment) is None


def test_a_worktree_outside_any_repository_gets_no_project_layer(server_elsewhere, tmp_path):
    assert ledger_core.check_op(_comment(str(tmp_path / "server"))) is None
    assert ledger_agent_ops.check(_followup(str(tmp_path / "server"))) is None


def test_the_client_names_its_folder_only_on_operation_writes(tmp_path, monkeypatch):
    folder = tmp_path / "a b"
    folder.mkdir()
    monkeypatch.chdir(folder)
    sent = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"revision": "r"}).encode()

    def urlopen(request, timeout):
        sent.append((request.full_url.rsplit("/", 1)[1], request.get_header("X-ledger-worktree")))
        return Reply()

    monkeypatch.setattr(ledger_link, "urlopen", urlopen)
    resources = client.ResourceClient("http://ledger", {})
    resources.request("demo", "phases")
    resources.request("demo", "operations", {"ops": []})
    assert sent == [("phases", None), ("operations", str(folder).replace(" ", "%20"))]
    assert schemas.WORKTREE_HEADER == "X-Ledger-Worktree"


def test_the_client_sends_no_folder_when_its_folder_is_gone(monkeypatch):
    def gone():
        raise FileNotFoundError

    monkeypatch.setattr(client.os, "getcwd", gone)
    assert client.writer_header() == {}


def test_the_trust_check_refuses_only_an_untrusted_repository_with_project_conditions(
    server_elsewhere, stranger, tmp_path
):
    with pytest.raises(ValueError) as refused:
        filter_check.trusted_folder(stranger)
    assert str(refused.value) == UNTRUSTED
    assert filter_check.trusted_folder(server_elsewhere) == server_elsewhere
    assert filter_check.trusted_folder(str(tmp_path / "server")) == str(tmp_path / "server")


def test_the_trust_check_trusts_the_owner_of_the_operators_bundle(server_elsewhere, stranger, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=bundle, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:stranger/bundle.git"], cwd=bundle, check=True)
    with pytest.raises(ValueError):
        filter_check.trusted_folder(stranger)
    monkeypatch.setattr("hooks.context.profile_chain.read_state", lambda: {"bundle": {"path": str(bundle)}})
    assert filter_check.trusted_folder(stranger) == stranger


def test_the_folder_check_validates_only_a_carried_folder():
    assert ledger_comments.worktree({}, True) is None
    assert ledger_comments.worktree({"worktree": 7}, False) is None
    assert ledger_comments.worktree({"worktree": "/w"}, True) == "/w"
    for folder in ("w", "", 7, None):
        with pytest.raises(ValueError) as refused:
            ledger_comments.worktree({"worktree": folder}, True)
        assert str(refused.value) == "worktree must be the writer's absolute folder path"


def test_the_operations_route_hands_the_header_folder_to_apply(monkeypatch):
    from email.message import Message
    from io import BytesIO

    seen = []
    monkeypatch.setattr(mutations, "apply", lambda *args: seen.append(args) or {"applied": []})
    raw = json.dumps({"ops": []}).encode()
    headers = Message()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(raw))
    headers["X-Ledger-Worktree"] = "/home/a%20b"
    handler = SimpleNamespace(command="POST", headers=headers, rfile=BytesIO(raw))
    services = SimpleNamespace(policy=SimpleNamespace(max_body=1000), ledger=SimpleNamespace(core=json))
    assert api_routes.ledger_operation(handler, services, "demo", "operations", "eng") == {"applied": []}
    assert seen == [(services, "demo", "eng", {"ops": []}, "/home/a b")]
