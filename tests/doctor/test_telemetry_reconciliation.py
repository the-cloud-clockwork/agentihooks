from scripts.doctor import traces
from tests.doctor.test_active_telemetry import LIMITS, NOW, _binding, _local, _record


def _reconciled(missing, drift=6):
    remote = {
        "trace": "tr-1",
        "fresh_ms": NOW - 330_000,
        "observations": 319,
        "reconciliation": {"accepted": 318, "missing": missing, "drift": drift},
    }
    return _binding(local=_local(accepted=324, accepted_at=NOW - 302_000), remote=remote)


def test_inflated_cursor_count_with_complete_identities_is_drift_instead_of_loss():
    found = traces.active(_record(_reconciled([])), LIMITS)
    assert [f.kind for f in found] == ["telemetry count drift"]
    assert found[0].measure == 6


def test_a_missing_accepted_identity_is_stale_even_beside_an_inflated_counter():
    found = traces.active(_record(_reconciled(["missing-1"])), LIMITS)
    stale = [f for f in found if f.kind == "telemetry stale"]
    assert len(stale) == 1
    assert "1 accepted observation identities missing from Langfuse" in stale[0].evidence


def test_identity_reconciliation_keeps_the_ingestion_grace():
    binding = _reconciled(["missing-1"], drift=0)
    binding["local"]["accepted_at"] = NOW - LIMITS.active_stale_seconds * 1000
    assert traces.active(_record(binding), LIMITS) == []


def test_an_unavailable_identity_check_cannot_turn_a_counter_into_loss():
    binding = _reconciled([])
    binding["remote"]["reconciliation"] = None
    assert traces.active(_record(binding), LIMITS) == []
