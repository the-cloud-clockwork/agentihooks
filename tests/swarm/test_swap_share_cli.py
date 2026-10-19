import json

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401


def test_set_stores_the_swap_use_share_and_reports_it(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "swap-full-share=0.8") == 0
    assert store.config("sw").swap_full_share == 0.8
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["swap_full_share"] == 0.8
    assert cli.scaling_value("swap-full-share", "0.5") == 0.5


@pytest.mark.parametrize(
    ("pair", "reason"),
    [
        ("swap-full-share=x", "swap-full-share takes a number, the share of swap in use"),
        ("swap-full-share=2", "swap full share must be above 0 and at most 1"),
        ("load-low=x", "load-low takes a number, the one minute load per CPU"),
    ],
)
def test_set_refuses_a_bad_swap_use_share(env, capsys, pair, reason):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    before = store.config("sw")
    assert run("sw", "set", pair) == 1
    assert capsys.readouterr().err.strip() == f"swarm: {reason}"
    assert store.config("sw") == before
