import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.profiles import bundle_copy, bundles, render
from tests.test_user_bundle_install import _isolate_stamp, _write, world  # noqa: F401


def _commit(root: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def three(world):  # noqa: F811
    shared = world["root"].parent / "shared"
    _write(shared / "profiles" / ".keep", "")
    _write(shared / ".claude" / "rules" / "team.md", "TEAM\n")
    _write(world["root"] / ".claude" / "rules" / "core.md", "CORE\n")
    _write(world["user"] / ".claude" / "rules" / "mine.md", "MINE\n")
    found = {**world, "shared": shared}
    found["commits"] = {name: _commit(found[name]) for name in ("root", "shared", "user")}
    return found


def test_a_revision_splits_into_root_shared_user_and_the_two_part_form_has_no_shared_field():
    assert bundle_copy.split_revision("r") == ("r", None, "")
    assert bundle_copy.split_revision("r:u") == ("r", None, "u")
    assert bundle_copy.split_revision("r:s:u") == ("r", "s", "u")


def test_a_three_part_revision_with_an_empty_user_never_parses_as_root_user():
    assert bundle_copy.split_revision("r:s:") == ("r", "s", "")
    assert bundle_copy.split_revision("r:s") == ("r", None, "s")


def test_a_revision_with_more_than_three_parts_is_refused():
    with pytest.raises(ValueError, match="revision"):
        bundle_copy.split_revision("a:b:c:d")


def test_recording_pins_the_shared_bundle_beside_the_root_and_the_user_bundle(three, tmp_path):
    commits = three["commits"]
    out = tmp_path / "out"

    with bundle_copy.recorded(
        three["root"],
        commits["root"],
        out,
        three["user"],
        commits["user"],
        shared=three["shared"],
        shared_revision=commits["shared"],
    ):
        pinned = bundle_copy.current_shared()
        assert pinned is not None and pinned != three["shared"] and pinned.is_relative_to(out)
        assert (pinned / ".claude" / "rules" / "team.md").read_text() == "TEAM\n"
        assert bundle_copy.identity(pinned / ".claude" / "rules" / "team.md") == (
            three["shared"] / ".claude" / "rules" / "team.md"
        )

    assert bundle_copy.current_shared() is None


def test_a_recorded_shared_commit_without_a_linked_shared_bundle_is_refused(three, tmp_path):
    with pytest.raises(ValueError, match="recorded shared bundle commit .* no shared bundle is linked"):
        with bundle_copy.recorded(
            three["root"], three["commits"]["root"], tmp_path / "out", shared_revision=three["commits"]["shared"]
        ):
            pass


def test_a_root_user_recording_is_unchanged(three, tmp_path):
    with bundle_copy.recorded(
        three["root"], three["commits"]["root"], tmp_path / "out", three["user"], three["commits"]["user"]
    ):
        assert bundle_copy.current_user() is not None
        assert bundle_copy.current_shared() is None


def _record_calls(monkeypatch, tmp_path):
    seen = {}

    @contextmanager
    def fake_recorded(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        yield

    monkeypatch.setattr(render.bundle_copy, "recorded", fake_recorded)
    monkeypatch.setattr(render, "render_claude", lambda name, **kwargs: tmp_path)
    return seen


def test_rendering_a_three_part_revision_pins_all_three_and_an_empty_user_field_means_no_user_bundle(
    three, monkeypatch, tmp_path
):
    bundles.link(three["root"])
    bundles.link(three["shared"], shared=True)
    seen = _record_calls(monkeypatch, tmp_path)
    commits = three["commits"]

    render.render("claude", "tiny", bundle_revision=f"{commits['root']}:{commits['shared']}:")

    assert seen["args"][1] == commits["root"]
    assert seen["args"][4] == ""
    assert seen["kwargs"]["shared_revision"] == commits["shared"]
    assert seen["kwargs"]["shared"] == three["shared"]


def test_a_host_without_the_shared_bundle_warns_and_renders_without_it_instead_of_refusing(
    three, monkeypatch, tmp_path, capsys
):
    bundles.link(three["root"])
    seen = _record_calls(monkeypatch, tmp_path)
    commits = three["commits"]

    render.render("claude", "tiny", bundle_revision=f"{commits['root']}:{commits['shared']}:")

    err = capsys.readouterr().err
    assert f"recorded shared bundle commit {commits['shared']}" in err
    assert "no shared bundle is linked on this host; rendering without it" in err
    assert seen["args"][1] == commits["root"]
    assert not seen["kwargs"].get("shared_revision")


def test_a_three_part_revision_with_an_empty_shared_field_is_refused_while_a_shared_bundle_is_linked(
    three, monkeypatch, tmp_path
):
    bundles.link(three["root"])
    bundles.link(three["user"], user=True)
    bundles.link(three["shared"], shared=True)
    _record_calls(monkeypatch, tmp_path)
    commits = three["commits"]

    with pytest.raises(ValueError, match="shared bundle"):
        render.render("claude", "tiny", bundle_revision=f"{commits['root']}::{commits['user']}")


def test_a_pre_link_two_part_pin_renders_with_the_current_shared_commit_and_stamps_it(
    three, monkeypatch, tmp_path, capsys
):
    _write(three["shared"] / ".claude" / "rules" / "late.md", "LATE\n")
    current = _commit(three["shared"])
    assert current != three["commits"]["shared"]
    bundles.link(three["root"])
    bundles.link(three["shared"], shared=True)
    _isolate_stamp(monkeypatch)
    seen = {}

    def fake_render(name, **kwargs):
        pinned = bundle_copy.current_shared()
        seen["stamp"] = render._stamp(render._bundle(), [])
        seen["pinned"] = pinned
        seen["late"] = (pinned / ".claude" / "rules" / "late.md").read_text()
        return tmp_path

    monkeypatch.setattr(render, "render_claude", fake_render)

    render.render("claude", "tiny", bundle_revision=three["commits"]["root"])

    err = capsys.readouterr().err
    assert "the launch recorded no shared bundle commit" in err
    assert f"rendering with its current commit {current}" in err
    assert seen["late"] == "LATE\n"
    assert seen["pinned"] != three["shared"]
    assert seen["stamp"]["shared_bundle_commit"] == current
    assert seen["stamp"]["bundle_commit"] == three["commits"]["root"]


def test_an_unpinnable_shared_bundle_round_trips_through_the_revision_and_renders_without_it(
    three, monkeypatch, tmp_path, capsys
):
    from scripts.swarm import overlays

    plain = three["root"].parent / "plain"
    plain.mkdir()
    bundles.link(three["root"])
    bundles.link(plain, shared=True)
    seen = _record_calls(monkeypatch, tmp_path)
    revision = overlays.revision()
    capsys.readouterr()

    assert revision == f"{three['commits']['root']}:-:"
    assert bundle_copy.split_revision(revision) == (three["commits"]["root"], bundle_copy.UNPINNED, "")

    render.render("claude", "tiny", bundle_revision=revision)

    assert "recorded no commit for the shared bundle; rendering without it" in capsys.readouterr().err
    assert seen["args"][1] == three["commits"]["root"]
    assert not seen["kwargs"].get("shared_revision")


def test_an_unpinned_shared_field_never_parses_as_root_user():
    assert bundle_copy.split_revision("r:-:") == ("r", bundle_copy.UNPINNED, "")
    assert bundle_copy.split_revision("r:-:u") == ("r", bundle_copy.UNPINNED, "u")
    assert bundle_copy.split_revision("r:-") == ("r", None, "-")


def test_rendering_a_root_user_revision_without_a_shared_bundle_is_unchanged(three, monkeypatch, tmp_path):
    bundles.link(three["root"])
    bundles.link(three["user"], user=True)
    seen = _record_calls(monkeypatch, tmp_path)
    commits = three["commits"]

    render.render("claude", "tiny", bundle_revision=f"{commits['root']}:{commits['user']}")

    assert seen["args"][1] == commits["root"]
    assert seen["args"][4] == commits["user"]
    assert not seen["kwargs"].get("shared_revision")
