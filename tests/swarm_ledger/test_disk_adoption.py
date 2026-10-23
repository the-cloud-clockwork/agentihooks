from scripts.swarm_ledger import ledger_artifacts, ledger_server
from scripts.swarm_ledger import ledger_core as core


def test_the_server_adopts_every_ledger_folder_past_a_failing_one(monkeypatch, capsys):
    for name in ("b.media", "a.media", "c.txt"):
        (core.LEDGER_DIR / name).mkdir(parents=True)
    seen = []

    def adopt(slug):
        seen.append(slug)
        if slug == "a":
            raise OSError("unreadable")

    monkeypatch.setattr(ledger_artifacts, "adopt_disk", adopt)
    ledger_server.adopt_disk_files()
    assert seen == ["a", "b"]
    assert capsys.readouterr().err == "disk adoption a: unreadable\n"
