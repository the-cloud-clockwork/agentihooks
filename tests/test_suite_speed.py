from scripts import deps_preflight, install


def test_suite_never_runs_the_real_deps_preflight(tmp_path, monkeypatch):
    (tmp_path / "deps.json").write_text("{}")
    monkeypatch.setattr(install, "_get_bundle_path", lambda: tmp_path)
    assert deps_preflight.manifest_path() is None
