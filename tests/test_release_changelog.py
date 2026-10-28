import io
import json
import subprocess
from urllib.error import HTTPError, URLError

import pytest

from scripts import release_changelog as rc

pytestmark = pytest.mark.unit


def _words(n: int) -> str:
    return " ".join(["word"] * n)


def _change(number=7, **fields) -> rc.Change:
    return rc.Change(
        **{
            "sha": "a" * 40,
            "number": number,
            "title": "Do it",
            "date": "2026-10-28",
            "stat": "1 file changed",
            **fields,
        }
    )


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for var, value in {
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@x",
        "GIT_AUTHOR_DATE": "2026-10-27T10:00:00Z",
        "GIT_COMMITTER_DATE": "2026-10-27T10:00:00Z",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(var, value)
    _git(tmp_path, "init", "-q", "-b", "dev")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _commit(repo, subject, name="f"):
    (repo / name).write_text(subject + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _promote(repo, graded, tag):
    tree = _git(repo, "rev-parse", f"{graded}^{{tree}}")
    sha = _git(
        repo, "commit-tree", tree, "-m", f"Promote green dev to main\n\nGraded dev revision: {graded}\n\nTests: x"
    )
    _git(repo, "tag", tag, sha)
    return sha


@pytest.mark.parametrize("count", [20, 21, 99, 100])
def test_check_words_accepts_twenty_to_one_hundred_and_collapses_whitespace(count):
    assert rc.check_words("  " + _words(count).replace(" ", " \n ", 1) + "\n") == _words(count)


@pytest.mark.parametrize("count", [0, 19, 101])
def test_check_words_refuses_outside_the_bounds(count):
    with pytest.raises(rc.WordCountError, match=f"^{count} words, need 20 to 100$"):
        rc.check_words(_words(count))


def test_word_count_splits_on_any_whitespace():
    assert rc.word_count("a\tb\nc  d") == 4


def test_write_entry_returns_the_first_valid_answer_without_retry():
    calls = []
    assert rc.write_entry(_change(), lambda c: calls.append(c) or _words(30)) == _words(30)
    assert len(calls) == 1


@pytest.mark.parametrize("first", [rc.WordCountError, rc.ModelError])
def test_write_entry_retries_once_after_a_failure(first, capsys):
    answers = iter([first, _words(25)])

    def ask(change):
        answer = next(answers)
        if answer is first:
            raise first("bad")
        return answer

    assert rc.write_entry(_change(), ask) == _words(25)
    assert capsys.readouterr().err == "#7 attempt 1: bad\n"


def test_write_entry_fails_after_two_short_answers(capsys):
    calls = []
    with pytest.raises(rc.WordCountError, match="^5 words"):
        rc.write_entry(_change(), lambda c: calls.append(c) or _words(5))
    assert len(calls) == 2
    assert capsys.readouterr().err == "#7 attempt 1: 5 words, need 20 to 100\n#7 attempt 2: 5 words, need 20 to 100\n"


def test_previous_tag_orders_by_version_and_ignores_prereleases():
    tags = ["v2.17.9", "v2.17.10", "v2.17.100", "v2.17.11rc1", "latest", "v2.16.200"]
    assert rc.previous_tag("v2.17.100", tags) == "v2.17.10"
    assert rc.previous_tag("v2.17.10", tags) == "v2.17.9"
    assert rc.previous_tag("v2.17.9", tags) == "v2.16.200"
    assert rc.previous_tag("v2.16.200", tags) == ""


def test_graded_revision_reads_the_promotion_commit(repo):
    graded = _commit(repo, "One (#1)")
    _promote(repo, graded, "v1.0.1")
    assert rc.graded_revision("v1.0.1") == graded


def test_graded_revision_refuses_a_commit_without_one(repo):
    _commit(repo, "One (#1)")
    with pytest.raises(ValueError, match="^HEAD records no graded dev revision$"):
        rc.graded_revision("HEAD")


def test_collect_changes_lists_first_parent_changes_newest_first(repo):
    base = _commit(repo, "Base (#1)")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, "Side work", "s")
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Add a thing (#12)", "side")
    first = _git(repo, "rev-parse", "HEAD")
    second = _commit(repo, "Direct fix", "h")
    changes = rc.collect_changes(base, second)
    assert [(c.sha, c.number, c.title, c.date) for c in changes] == [
        (second, None, "Direct fix", "2026-10-27"),
        (first, 12, "Add a thing", "2026-10-27"),
    ]
    assert changes[0].stat == "1 file changed, 1 insertion(+)"


def test_collect_changes_without_previous_reads_the_whole_history(repo):
    _commit(repo, "Base (#1)")
    head = _commit(repo, "Two (#2)", "g")
    assert [c.number for c in rc.collect_changes("", head)][:1] == [2]


def test_fetch_pull_requests_batches_and_fills_author_and_body(monkeypatch):
    monkeypatch.setattr(rc, "BATCH", 2)
    queries = []

    def graphql(query):
        queries.append(query)
        return {"repository": {"p1": {"author": {"login": "ann"}, "body": "why"}, "p2": {"author": None, "body": None}}}

    changes = [_change(1), _change(None), _change(2), _change(3)]
    rc.fetch_pull_requests(changes, "org/repo", graphql)
    assert [(c.author, c.body) for c in changes] == [("ann", "why"), ("", ""), ("ghost", ""), ("ghost", "")]
    assert queries == [
        '{ repository(owner: "org", name: "repo") { p1: pullRequest(number: 1) { author { login } body } '
        "p2: pullRequest(number: 2) { author { login } body } } }",
        '{ repository(owner: "org", name: "repo") { p3: pullRequest(number: 3) { author { login } body } } }',
    ]


def test_prompt_carries_title_number_stat_and_a_bounded_body(monkeypatch):
    monkeypatch.setattr(rc, "BODY_CHARS", 3)
    system, user = rc.prompt(_change(body="abcdef"))
    assert system == {"role": "system", "content": rc.SYSTEM}
    assert "20 to 100 words" in rc.SYSTEM
    assert user == {
        "role": "user",
        "content": "Title: Do it\nPull request: #7\nDiff stat: 1 file changed\n\nDescription:\nabc",
    }


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ask_model_posts_chat_completion_with_the_key_reference():
    seen = {}

    def opener(request, timeout):
        seen.update(
            url=request.full_url,
            auth=request.get_header("Authorization"),
            type=request.get_header("Content-type"),
            body=json.loads(request.data),
            timeout=timeout,
        )
        return _Response(json.dumps({"choices": [{"message": {"content": "text"}}]}).encode())

    assert rc.ask_model(_change(), "http://gw/", "k1", "sonnet", opener) == "text"
    assert seen["url"] == "http://gw/v1/chat/completions"
    assert seen["auth"] == "Bearer k1"
    assert seen["type"] == "application/json"
    assert seen["timeout"] == rc.TIMEOUT_S
    assert seen["body"] == {"model": "sonnet", "messages": rc.prompt(_change()), "max_tokens": 400}


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (HTTPError("u", 401, "no", {}, io.BytesIO(b"secret k1")), "^sonnet: HTTP 401$"),
        (URLError("down"), "^sonnet: URLError$"),
        (TimeoutError(), "^sonnet: TimeoutError$"),
    ],
)
def test_ask_model_reports_transport_failures_without_the_key(error, message):
    def opener(request, timeout):
        raise error

    with pytest.raises(rc.ModelError, match=message):
        rc.ask_model(_change(), "http://gw", "k1", "sonnet", opener)


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"choices": []}', b'{"choices": [null]}'])
def test_ask_model_refuses_an_unreadable_answer(payload):
    with pytest.raises(rc.ModelError, match="^sonnet: unreadable response$"):
        rc.ask_model(_change(), "http://gw", "k1", "sonnet", lambda request, timeout: _Response(payload))


def test_render_release_names_date_hash_pull_request_and_author():
    entries = [(_change(8338, author="bot"), "Explains it."), (_change(None, sha="b" * 40, title="Fix"), "Other.")]
    assert rc.render_release("v1.2.3", "2026-10-28", entries) == (
        "## v1.2.3 · 2026-10-28\n\n"
        "### 2026-10-28 · aaaaaaaaaaaa · #8338 · @bot\n\n**Do it**\n\nExplains it.\n\n"
        "### 2026-10-28 · bbbbbbbbbbbb · direct commit · @unknown\n\n**Fix**\n\nOther.\n"
    )


def test_render_release_without_changes_says_so():
    assert rc.render_release("v1.2.3", "2026-10-28", []) == "## v1.2.3 · 2026-10-28\n\nNo merged changes.\n"


def test_prepend_release_starts_a_new_changelog():
    assert rc.prepend_release("", "## v1 · d\n\nx\n") == f"{rc.TITLE}\n\n{rc.INTRO}\n\n## v1 · d\n\nx\n"


def test_prepend_release_puts_the_newest_release_first():
    older = rc.prepend_release("", "## v1 · d\n\nx\n")
    assert (
        rc.prepend_release(older, "## v2 · e\n\ny\n")
        == f"{rc.TITLE}\n\n{rc.INTRO}\n\n## v2 · e\n\ny\n\n## v1 · d\n\nx\n"
    )


def test_prepend_release_keeps_every_older_release():
    two = rc.prepend_release(rc.prepend_release("", "## v1 · d\n\nx\n"), "## v2 · e\n\ny\n")
    assert rc.prepend_release(two, "## v3 · f\n\nz\n") == (
        f"{rc.TITLE}\n\n{rc.INTRO}\n\n## v3 · f\n\nz\n\n## v2 · e\n\ny\n\n## v1 · d\n\nx\n"
    )


def test_gh_graphql_runs_the_query_through_gh(monkeypatch):
    seen = []
    monkeypatch.setattr(rc, "run", lambda *command: seen.append(command) or '{"data": {"repository": {}}}')
    assert rc.gh_graphql("{ q }") == {"repository": {}}
    assert seen == [("gh", "api", "graphql", "-f", "query={ q }")]


def test_main_needs_a_tag_changelog_and_section():
    with pytest.raises(SystemExit):
        rc.main(["v1", "out"])


def test_prepend_release_keeps_a_release_already_written():
    existing = rc.prepend_release("", "## v1 · d\n\nx\n")
    assert rc.prepend_release(existing, "## v1 · d\n\nchanged\n") == existing


def test_main_writes_the_section_and_the_changelog_for_a_tag(repo, monkeypatch, tmp_path):
    _promote(repo, _commit(repo, "Base (#1)"), "v1.0.1")
    graded = _commit(repo, "Add the changelog (#5)", "g")
    _promote(repo, graded, "v1.0.2")
    asked = []

    def opener(request, timeout):
        asked.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)["model"]))
        return _Response(json.dumps({"choices": [{"message": {"content": _words(22)}}]}).encode())

    monkeypatch.setattr(rc, "urlopen", opener)
    monkeypatch.setattr(
        rc, "gh_graphql", lambda query: {"repository": {"p5": {"author": {"login": "dev"}, "body": "b"}}}
    )
    monkeypatch.setenv(rc.KEY_VAR, "k2")
    monkeypatch.delenv(rc.URL_VAR, raising=False)
    monkeypatch.setenv(rc.MODEL_VAR, "")
    section, changelog = tmp_path / "section.md", tmp_path / "out" / "RELEASES.md"
    changelog.parent.mkdir()
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    assert rc.main(["v1.0.2", str(changelog), str(section)]) == 0
    expected = f"## v1.0.2 · 2026-10-27\n\n### 2026-10-27 · {graded[:12]} · #5 · @dev\n\n**Add the changelog**\n\n{_words(22)}\n"
    assert section.read_text() == expected
    assert changelog.read_text() == f"{rc.TITLE}\n\n{rc.INTRO}\n\n{expected}"
    assert asked == [(rc.DEFAULT_URL + "/v1/chat/completions", "Bearer k2", rc.DEFAULT_MODEL)]
