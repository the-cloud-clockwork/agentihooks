from scripts.swarm import host_budget
from scripts.swarm.host_budget import HostSample, Thresholds

LIMITS = Thresholds(load_high=1.5, load_low=1.0, memory_per_agent_mb=700)


def _sample(load1: float, available_mb: int, agents: int = 0, cpus: int = 20) -> HostSample:
    return HostSample(load1=load1, cpus=cpus, available_mb=available_mb, agents=agents)


def test_high_load_gives_zero_room():
    decision = host_budget.room(_sample(load1=40.0, available_mb=16000, agents=10), LIMITS, previous=5)

    assert decision.room == 0
    assert "above the high watermark" in decision.reason


def test_low_load_with_free_memory_gives_room():
    decision = host_budget.room(_sample(load1=10.0, available_mb=7000, agents=10), LIMITS, previous=0)

    assert decision.room == 10
    assert "below the low watermark" in decision.reason


def test_band_between_watermarks_holds_previous_room():
    decision = host_budget.room(_sample(load1=24.0, available_mb=16000, agents=10), LIMITS, previous=3)

    assert decision.room == 3
    assert "previous room of 3 holds" in decision.reason


def test_band_without_previous_room_gives_zero():
    assert host_budget.room(_sample(load1=24.0, available_mb=16000), LIMITS).room == 0


def test_memory_alone_limits_room():
    decision = host_budget.room(_sample(load1=0.0, available_mb=2100), LIMITS, previous=8)

    assert decision.room == 3
    assert "memory" in decision.reason


def test_memory_caps_held_room_in_band():
    decision = host_budget.room(_sample(load1=24.0, available_mb=1400, agents=10), LIMITS, previous=5)

    assert decision.room == 2
    assert "memory" in decision.reason


def test_projected_load_limits_room_below_low_watermark():
    decision = host_budget.room(_sample(load1=18.0, available_mb=16000, agents=18), LIMITS, previous=0)

    assert decision.room == 12
    assert "load" in decision.reason


def test_default_thresholds_name_the_measured_memory_per_agent():
    assert Thresholds().memory_per_agent_mb == host_budget.MEMORY_PER_AGENT_MB == 700


def test_read_host_reads_load_memory_and_agents(tmp_path, monkeypatch):
    (tmp_path / "loadavg").write_text("12.50 10.00 8.00 3/900 4242\n")
    (tmp_path / "meminfo").write_text("MemTotal:       20480000 kB\nMemAvailable:    5120000 kB\n")
    monkeypatch.setattr(host_budget.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(host_budget.account_sessions, "live_sessions", lambda proc: {1: "a", 2: "b"})
    monkeypatch.setattr(host_budget.account_sessions, "live_codex_sessions", lambda proc: 1)

    assert host_budget.read_host(tmp_path) == HostSample(load1=12.5, cpus=16, available_mb=5000, agents=3)
