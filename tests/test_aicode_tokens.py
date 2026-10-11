from pathlib import Path

import pytest

from scripts import aicode_tokens as tokens

START, END = tokens.START, tokens.END


def test_a_slug_turns_hyphens_into_underscores_and_keeps_letters_and_digits():
    assert tokens.slug("nestorcolt-gmail-business") == "nestorcolt_gmail_business"
    assert tokens.slug("Team2") == "Team2"
    assert tokens.slug("2team") == "2team"


@pytest.mark.parametrize(
    "raw", ["", "_x", "-x", "a.b", "a b", "a/b", "ñu", "status", "help", "default", "api", "Status", "API"]
)
def test_a_slug_refuses_empty_underscore_leading_reserved_and_other_characters(raw):
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
    text = f"{START}\nexport A=old\n{END}\nmid\n{START}\nexport A=older\nexport B=2\n{END}\n"
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
