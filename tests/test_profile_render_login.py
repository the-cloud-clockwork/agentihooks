"""Rendered Claude profile homes never link the login credentials file to the operator login home."""

from pathlib import Path

import pytest

from scripts.profiles import render
from tests.test_profile_render import _write
from tests.test_profile_render import world as world

LOGIN = ".credentials.json"


@pytest.mark.feature("render-no-login-credentials")
def test_a_rendered_engineer_home_has_no_credentials_link_to_the_login_home(world: dict[str, Path]) -> None:
    _write(world["home"] / ".claude" / LOGIN, "fixture")
    out = render.render_claude("rb-role")
    assert out is not None
    assert not (out / LOGIN).is_symlink()
    assert not (out / LOGIN).exists()


@pytest.mark.feature("render-no-login-credentials")
def test_the_next_render_replaces_a_home_holding_a_credentials_link(world: dict[str, Path]) -> None:
    login = _write(world["home"] / ".claude" / LOGIN, "fixture")
    out = render.render_claude("rb-role")
    assert out is not None
    (out / LOGIN).symlink_to(login)
    assert render.render_claude("rb-role") is not None
    rendered = render.profile_dir("rb-role")
    assert rendered is not None
    assert rendered / "claude" != out
    assert not (rendered / "claude" / LOGIN).is_symlink()
    assert render.render_claude("rb-role") is None
