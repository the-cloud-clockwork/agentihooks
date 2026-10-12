import os
from pathlib import Path

import pytest

from hooks.context import account_sessions
from scripts import account_names
from scripts import aicode_tokens as tokens

START, END = tokens.START, tokens.END


def test_a_slug_turns_hyphens_into_underscores_and_lowercases_letters():
    assert tokens.slug("nestorcolt-gmail-business") == "nestorcolt_gmail_business"
    assert tokens.slug("Team2") == "team2"
    assert tokens.slug("2team") == "2team"
    assert tokens.slug("ncgmaTCC") == "ncgmatcc"


@pytest.mark.parametrize("raw", ["", "_x", "-x", ".", "ñu", "status", "help", "default", "api", "Status", "API"])
def test_a_slug_refuses_empty_underscore_leading_and_reserved_results(raw):
    with pytest.raises(tokens.SlugError):
        tokens.slug(raw)


def test_the_rc_file_is_zshrc_on_macos_and_bashrc_elsewhere():
    assert tokens.rc_file("darwin") == Path.home() / ".zshrc"
    assert tokens.rc_file("linux") == Path.home() / ".bashrc"


def test_an_upsert_appends_the_block_at_the_end_when_it_is_absent():
    assert tokens.upsert("alias ll='ls -l'", {"A": "1"}) == f"alias ll='ls -l'\n{START}\nexport A=1\n{END}\n"
    assert tokens.upsert("", {"A": "1"}) == f"{START}\nexport A=1\n{END}\n"


def test_an_upsert_replaces_in_place_adds_missing_names_and_keeps_every_other_line():
    text = f"export A=outside\n{START}\nexport A=old\n# note\nexport B=2\n{END}\ntail\n"
    assert tokens.upsert(text, {"A": "new", "C": "3"}) == (
        f"export A=outside\n{START}\nexport A=new\n# note\nexport B=2\nexport C=3\n{END}\ntail\n"
    )


def test_an_upsert_drops_stale_names_inside_the_block_only():
    text = f"export OLD=keep\n{START}\nexport OLD=x\nexport B=2\n{END}\n"
    assert tokens.upsert(text, {"NEW": "1"}, stale=("OLD",)) == (
        f"export OLD=keep\n{START}\nexport B=2\nexport NEW=1\n{END}\n"
    )


def test_an_upsert_without_a_closing_marker_appends_a_fresh_block():
    assert tokens.upsert(f"{START}\nexport A=0\n", {"A": "1"}).endswith(f"{START}\nexport A=1\n{END}\n")


def test_an_upsert_quotes_a_value_the_shell_would_split():
    assert "export A='a b'\n" in tokens.upsert("", {"A": "a b"})


def test_write_rc_replaces_the_file_atomically_and_keeps_its_mode(tmp_path):
    folder = tmp_path / "rc"
    folder.mkdir()
    rc = folder / ".bashrc"
    rc.write_text("x\n")
    rc.chmod(0o644)
    tokens.write_rc(rc, {"A": "1"})
    assert rc.read_text() == f"x\n{START}\nexport A=1\n{END}\n"
    assert rc.stat().st_mode & 0o777 == 0o644
    assert [path.name for path in folder.iterdir()] == [".bashrc"]


def test_write_rc_writes_through_a_symlinked_rc_file(tmp_path):
    target = tmp_path / "dotfiles" / "bashrc"
    target.parent.mkdir()
    target.write_text("x\n")
    link = tmp_path / ".bashrc"
    link.symlink_to(target)
    tokens.write_rc(link, {"A": "1"})
    assert link.is_symlink()
    assert target.read_text() == f"x\n{START}\nexport A=1\n{END}\n"


def test_an_upsert_leaves_a_duplicate_block_without_the_export():
    text = f"{START}\nexport A=old\n{END}\nmid\n{START}\nexport B=2\nexport A=older\n{END}\n"
    assert tokens.upsert(text, {"A": "new"}) == f"{START}\nexport A=new\n{END}\nmid\n{START}\nexport B=2\n{END}\n"


def test_write_rc_creates_a_missing_file_readable_only_by_its_owner(tmp_path):
    rc = tmp_path / ".zshrc"
    tokens.write_rc(rc, {"A": "1"})
    assert rc.stat().st_mode & 0o777 == 0o600


def test_update_existing_writes_only_a_file_that_already_has_the_block(tmp_path):
    rc = tmp_path / ".bashrc"
    assert tokens.update_existing(rc, {"A": "1"}) is False
    assert not rc.exists()
    rc.write_text("plain\n")
    assert tokens.update_existing(rc, {"A": "1"}) is False
    assert rc.read_text() == "plain\n"
    rc.write_text(f"{START}\nexport A=0\n{END}\n")
    assert tokens.update_existing(rc, {"A": "1"}) is True
    assert rc.read_text() == f"{START}\nexport A=1\n{END}\n"


def test_a_refused_slug_names_the_rule_and_every_reserved_word():
    with pytest.raises(tokens.SlugError) as refused:
        tokens.slug("Status")
    assert str(refused.value) == (
        "slug is lowercased, '@' becomes 'at' and every other character outside a-z and 0-9 becomes '_'; "
        "the result must not start with '_' and must not be api, default, help, status: 'Status'"
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("nestor.colt@gmail.com", "nestor_coltatgmail_com"),
        ("first.last+tag@example.co.uk", "first_last_tagatexample_co_uk"),
        ("a__b--c", "a__b__c"),
        ("a b/c", "a_b_c"),
        ("team.", "team_"),
        ("@x", "atx"),
    ],
)
def test_an_email_follows_the_account_names_rule_one_character_for_one(raw, expected):
    assert tokens.slug(raw) == expected
    assert account_sessions.TOKEN_PREFIX + tokens.slug(raw) == account_names.token_env(raw)


def test_the_slug_note_shows_an_email_turned_into_a_variable_name():
    assert tokens.SLUG_NOTE.format(prefix="AH_CC_TOKEN_") == (
        "Note: use the email of the target account as the slug, so each token is recognisable at a glance "
        "(nestor.colt@gmail.com becomes AH_CC_TOKEN_nestor_coltatgmail_com)."
    )


@pytest.mark.parametrize(
    "environ, expected",
    [({"AGENTIHOOKS_AICODE_TOKEN_REFRESH": value}, True) for value in ("true", "True", " true ")]
    + [({"AGENTIHOOKS_AICODE_TOKEN_REFRESH": value}, False) for value in ("false", "yes", "1", "")]
    + [({}, False)],
)
def test_the_refresh_authority_is_the_flag_set_to_true_in_any_case(environ, expected):
    assert tokens.refresh_authority(environ) is expected


def test_an_end_marker_before_any_start_marker_is_not_a_block():
    text = f"{END}\nx\n{START}\nexport A=0\n{END}\n"
    assert tokens.upsert(text, {"A": "1"}) == f"{END}\nx\n{START}\nexport A=1\n{END}\n"
    assert tokens.has_block(f"{END}\n{START}\n") is False


def test_a_stray_end_marker_after_a_block_opens_nothing():
    text = f"{START}\nexport A=0\n{END}\n{END}\n"
    assert tokens.upsert(text, {"A": "1"}) == f"{START}\nexport A=1\n{END}\n{END}\n"


def test_an_upsert_keeps_one_export_when_the_block_repeats_the_name():
    text = f"{START}\nexport A=1\n# keep\nexport A=2\n{END}\n"
    assert tokens.upsert(text, {"A": "3"}) == f"{START}\nexport A=3\n# keep\n{END}\n"


def test_write_rc_writes_the_whole_block_into_a_new_file(tmp_path):
    rc = tmp_path / ".bashrc"
    tokens.write_rc(rc, {"A": "1"})
    assert rc.read_text() == f"{START}\nexport A=1\n{END}\n"


def test_the_atomic_write_stages_a_hidden_file_beside_the_target(tmp_path, monkeypatch):
    seen = []
    real = tokens.tempfile.mkstemp

    def mkstemp(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(tokens.tempfile, "mkstemp", mkstemp)
    target = tmp_path / "auth.json"
    tokens.atomic_write(target, b"{}", 0o600)
    assert seen == [{"dir": tmp_path, "prefix": ".auth.json."}]
    assert target.read_bytes() == b"{}" and target.stat().st_mode & 0o777 == 0o600


def test_a_failed_atomic_write_leaves_the_target_and_no_staged_file(tmp_path, monkeypatch):
    tmp_path = tmp_path / "account"
    tmp_path.mkdir()
    target = tmp_path / "auth.json"
    target.write_bytes(b"old")

    def refuse(source, destination):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(tokens.os, "replace", refuse)
    with pytest.raises(OSError):
        tokens.atomic_write(target, b"new", 0o600)
    assert [path.name for path in tmp_path.iterdir()] == ["auth.json"]
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize("written", [OSError(28, "No space left on device"), 1])
def test_a_failed_or_short_write_closes_the_staged_file_and_removes_it(written, tmp_path, monkeypatch):
    folder = tmp_path / "account"
    folder.mkdir()
    descriptors = []
    real = tokens.tempfile.mkstemp

    def mkstemp(**kwargs):
        found = real(**kwargs)
        descriptors.append(found[0])
        return found

    def write(fd, data):
        if isinstance(written, OSError):
            raise written
        return written

    monkeypatch.setattr(tokens.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(tokens, "_write", write)
    with pytest.raises(OSError) as raised:
        tokens.atomic_write(folder / "auth.json", b"{}", 0o600)
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert list(folder.iterdir()) == []
    if written == 1:
        assert str(raised.value).startswith("short write to ")
