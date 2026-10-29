import subprocess

import pytest

from tests import test_suite_speed as contracts


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_import_failure_reports_module_and_captured_output(tmp_path, monkeypatch, stream):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_broken.py").write_text(
        'import sys\ndependency = "fakeredis"\n'
        f'print("captured import detail", file=sys.{stream})\n'
        'raise RuntimeError("planted import exception")\n'
    )
    monkeypatch.setattr(contracts, "_ROOT", tmp_path)
    with pytest.raises((subprocess.CalledProcessError, pytest.fail.Exception)) as failure:
        contracts.fakeredis_modules.__wrapped__()
    report = str(failure.value)
    assert "tests.test_broken" in report
    assert "RuntimeError: planted import exception" in report
    assert "captured import detail" in report


def test_eager_import_failure_reports_responsible_module(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_eager.py").write_text(
        'import sys\nfrom types import ModuleType\nsys.modules["fakeredis"] = ModuleType("fakeredis")\n'
    )
    monkeypatch.setattr(contracts, "_ROOT", tmp_path)
    probe = contracts.fakeredis_modules.__wrapped__()
    with pytest.raises(AssertionError) as failure:
        contracts.test_fakeredis_test_modules_import_without_loading_redis(probe)
    assert "tests.test_eager" in str(failure.value)
