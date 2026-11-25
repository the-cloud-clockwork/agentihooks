from contextlib import closing
from pathlib import Path

import pytest

from scripts.swarm import metrics_outbox


@pytest.mark.parametrize("home", [None, ""], ids=["unset", "empty"])
@pytest.mark.feature("metrics-spool-configured-home")
def test_metrics_spool_keeps_the_default_home(
    home: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if home is None:
        monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_HOME", home)
    assert metrics_outbox.spool_path() == tmp_path / ".agentihooks" / "swarm" / "metrics-outbox.sqlite"


@pytest.mark.feature("metrics-spool-configured-home")
def test_metrics_spool_honours_the_configured_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "runtime"))
    assert metrics_outbox.spool_path() == tmp_path / "runtime" / "swarm" / "metrics-outbox.sqlite"


@pytest.mark.feature("metrics-spool-configured-home")
def test_metrics_spool_opens_under_the_configured_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "runtime"))
    expected = tmp_path / "runtime" / "swarm" / "metrics-outbox.sqlite"
    with closing(metrics_outbox.Outbox(metrics_outbox.spool_path(), None)) as box:
        assert box.path == expected
        assert expected.is_file()
        assert box.db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
