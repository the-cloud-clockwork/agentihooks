from scripts.swarm_ledger import ledger_server


def test_the_server_adopts_every_ledger_folder_past_a_failing_one(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    for name in ("b.media", "a.media", "c.txt"):
        (tmp_path / name).mkdir()
    seen = []

    def adopt(slug):
        seen.append(slug)
        if slug == "a":
            raise OSError("unreadable")

    monkeypatch.setattr(ledger_server.ledger_artifacts, "adopt_disk", adopt)
    ledger_server.adopt_disk_files()
    assert seen == ["a", "b"]
    assert capsys.readouterr().err == "disk adoption a: unreadable\n"
