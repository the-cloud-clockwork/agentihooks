import io
import urllib.error

import pytest

from scripts.swarm_ledger import hosted_probe, ledger, ledger_link, ledger_route

HOSTED = "https://hosted.example"
SLUG = "doctor-plan"


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    monkeypatch.delenv(hosted_probe.HOSTED_ENV, raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "remote")
    monkeypatch.setenv("SWARM_URL", HOSTED)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "workstation-ledger"))
    probed, read = [], []

    def serving(timeout=1, url=None):
        probed.append(url)
        return "/data"

    def resource(slug, path, **kwargs):
        read.append((slug, path))
        return {"title": "Doctor"}

    monkeypatch.setattr(ledger_link, "serving", serving)
    monkeypatch.setattr(ledger, "resource", resource)
    return probed, read


def test_a_hosted_server_serving_another_folder_gives_the_named_ledger_link(hosted):
    probed, read = hosted
    assert ledger_link.page_line(SLUG) == f"Ledger page: {HOSTED}/{SLUG} (open it to follow and steer the work)"
    assert probed == [HOSTED]
    assert read == [(SLUG, "metadata")]


def test_an_unavailable_hosted_server_is_named_without_starting_a_local_one(hosted, monkeypatch):
    monkeypatch.setattr(ledger_link, "serving", lambda timeout=1, url=None: None)
    line = ledger_link.page_line(SLUG)
    assert line == f"No ledger page link: the hosted ledger server {HOSTED} is not answering"


def test_a_hosted_server_refusing_the_credential_names_the_refusal(hosted, monkeypatch):
    def refused(slug, path, **kwargs):
        raise urllib.error.HTTPError(f"{HOSTED}/api", 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr(ledger, "resource", refused)
    line = ledger_link.page_line(SLUG)
    assert line == f"No ledger page link: {HOSTED} answered a read of ledger {SLUG} with HTTP 403"


def test_a_hosted_server_without_the_ledger_names_the_missing_ledger(hosted, monkeypatch):
    def missing(slug, path, **kwargs):
        raise urllib.error.HTTPError(f"{HOSTED}/api", 404, "Not Found", {}, io.BytesIO(b""))

    monkeypatch.setattr(ledger, "resource", missing)
    assert (
        ledger_link.page_line(SLUG) == f"No ledger page link: {HOSTED} answered a read of ledger {SLUG} with HTTP 404"
    )


def test_a_missing_hosted_credential_names_its_reason(hosted, monkeypatch):
    def unauthenticated(slug, path, **kwargs):
        raise ledger.Unauthenticated("unauthenticated: a remote ledger client needs a pinned agent identity")

    monkeypatch.setattr(ledger, "resource", unauthenticated)
    assert ledger_link.page_line(SLUG) == (
        f"No ledger page link: {HOSTED} refused a read of ledger {SLUG}: "
        "unauthenticated: a remote ledger client needs a pinned agent identity"
    )


def test_a_hosted_server_dropping_the_read_is_not_answering(hosted, monkeypatch):
    def dropped(slug, path, **kwargs):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(ledger, "resource", dropped)
    assert ledger_link.page_line(SLUG) == f"No ledger page link: the hosted ledger server {HOSTED} is not answering"


def test_a_local_server_serving_another_folder_is_still_refused(monkeypatch, tmp_path):
    monkeypatch.delenv(hosted_probe.HOSTED_ENV, raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.delenv("LEDGER_URL", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setenv("LEDGER_HOST", "127.0.0.1")
    monkeypatch.setenv("LEDGER_PORT", "9912")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "mine"))
    monkeypatch.setattr(ledger_link, "serving", lambda timeout=1, url=None: str(tmp_path / "other"))
    monkeypatch.setattr(ledger, "resource", lambda *a, **k: pytest.fail("a local link reads no hosted ledger"))
    line = ledger_link.page_line(SLUG)
    assert line.startswith(f"No ledger page link: http://127.0.0.1:9912 serves {tmp_path / 'other'}, not ")
    assert ledger_link.START in line


def test_a_local_link_probes_its_own_ledger_while_another_ledger_is_routed(monkeypatch, tmp_path):
    monkeypatch.delenv(hosted_probe.HOSTED_ENV, raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.delenv("LEDGER_URL", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.setenv("LEDGER_HOST", "127.0.0.1")
    monkeypatch.setenv("LEDGER_PORT", "9913")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "mine"))
    probed = []

    def serving(timeout=1, url=None):
        probed.append(url)
        return str(tmp_path / "mine")

    monkeypatch.setattr(ledger_link, "serving", serving)
    with ledger_route.scoped("another-plan", "https://another.example"):
        line = ledger_link.page_line(SLUG)
    assert line == f"Ledger page: http://127.0.0.1:9913/{SLUG} (open it to follow and steer the work)"
    assert probed == ["http://127.0.0.1:9913"]
