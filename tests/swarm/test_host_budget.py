from scripts.swarm import host_budget
from scripts.swarm.host_budget import HostSample, Thresholds
from scripts.swarm.store import SwarmConfig

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
    (tmp_path / "meminfo").write_text("MemTotal:       20480000 kB\nMemAvailable:    5120500 kB\n")
    monkeypatch.setattr(host_budget.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        host_budget.account_sessions, "live_sessions", lambda proc: {1: "a", 2: "b"} if proc == tmp_path else {}
    )
    monkeypatch.setattr(host_budget.account_sessions, "live_codex_sessions", lambda proc: 1 if proc == tmp_path else 0)

    assert host_budget.read_host(tmp_path) == HostSample(load1=12.5, cpus=16, available_mb=5000, agents=3)


def test_load_exactly_at_high_watermark_holds_previous_room():
    assert host_budget.room(_sample(load1=30.0, available_mb=16000, agents=10), LIMITS, previous=4).room == 4


def test_load_exactly_at_low_watermark_holds_previous_room():
    assert host_budget.room(_sample(load1=20.0, available_mb=16000, agents=10), LIMITS, previous=4).room == 4


def test_load_without_live_agents_leaves_memory_as_the_limit():
    assert host_budget.room(_sample(load1=5.0, available_mb=2800), LIMITS).room == 4


def test_memory_room_counts_only_whole_agents():
    assert host_budget.memory_room(_sample(load1=0.0, available_mb=2500), LIMITS) == 3
    assert host_budget.memory_room(_sample(load1=0.0, available_mb=500), LIMITS) == 0


def test_load_room_is_unknown_without_load():
    assert host_budget.load_room(_sample(load1=0.0, available_mb=16000, agents=5), LIMITS) is None


def test_load_room_never_goes_below_zero():
    assert host_budget.load_room(_sample(load1=2.0, available_mb=16000, agents=1, cpus=1), LIMITS) == 0


def test_single_light_agent_projects_room_from_its_load():
    decision = host_budget.room(_sample(load1=0.5, available_mb=16000, agents=1, cpus=1), LIMITS)

    assert decision.room == 2
    assert "one minute load 0.50 per CPU" in decision.reason


def test_heavy_single_agent_projects_no_room():
    assert host_budget.room(_sample(load1=1.9, available_mb=16000, agents=1, cpus=2), LIMITS).room == 0


def test_single_cpu_above_high_watermark_gives_zero_room():
    assert host_budget.room(_sample(load1=2.0, available_mb=16000, agents=1, cpus=1), LIMITS, previous=3).room == 0


def test_memory_equal_to_held_room_keeps_the_held_reason():
    decision = host_budget.room(_sample(load1=24.0, available_mb=2100, agents=10), LIMITS, previous=3)

    assert decision.room == 3
    assert "previous room of 3 holds" in decision.reason


def test_projection_equal_to_memory_names_memory():
    decision = host_budget.room(_sample(load1=18.0, available_mb=8400, agents=18), LIMITS)

    assert decision.room == 12
    assert "MB available memory fits 12" in decision.reason


def test_each_room_names_the_limit_that_set_it():
    assert host_budget.room(_sample(load1=40.0, available_mb=16000, agents=10), LIMITS, previous=5).limit == "load"
    assert host_budget.room(_sample(load1=24.0, available_mb=16000, agents=10), LIMITS, previous=3).limit == "load"
    assert host_budget.room(_sample(load1=24.0, available_mb=1400, agents=10), LIMITS, previous=5).limit == "memory"
    assert host_budget.room(_sample(load1=18.0, available_mb=16000, agents=18), LIMITS).limit == "load"
    assert host_budget.room(_sample(load1=0.0, available_mb=2100), LIMITS).limit == "memory"


def test_only_the_band_hold_is_marked_held():
    assert host_budget.room(_sample(load1=24.0, available_mb=16000, agents=10), LIMITS, previous=3).held is True
    assert host_budget.room(_sample(load1=24.0, available_mb=1400, agents=10), LIMITS, previous=5).held is False
    assert host_budget.room(_sample(load1=40.0, available_mb=16000, agents=10), LIMITS, previous=5).held is False
    assert host_budget.room(_sample(load1=0.0, available_mb=2100), LIMITS).held is False


def test_thresholds_read_the_swarm_config():
    config = SwarmConfig("sw", "/repo", 1, 0, load_high=2.5, load_low=1.25, memory_per_agent_mb=900)

    assert host_budget.thresholds(config) == Thresholds(load_high=2.5, load_low=1.25, memory_per_agent_mb=900)


def test_spawn_room_judges_a_sample_with_the_swarm_thresholds():
    config = SwarmConfig("sw", "/repo", 1, 0, load_high=2.0, load_low=1.5, memory_per_agent_mb=1000)
    sample = _sample(load1=4.0, available_mb=5000, agents=2, cpus=8)

    assert host_budget.spawn_room(config, sample, 2) == host_budget.room(sample, Thresholds(2.0, 1.5, 1000), 2)


def test_an_unknown_sample_has_no_room_limit_and_says_host_unknown():
    decision = host_budget.spawn_room(SwarmConfig("sw", "/repo", 1, 0), None, 4)

    assert decision == host_budget.Room(
        None, "host unknown: the process files cannot be read, so spawns pass", "unknown"
    )


def test_a_host_without_process_files_reads_as_unknown(tmp_path):
    assert host_budget.read_host(tmp_path) is None


def test_a_meminfo_without_available_memory_reads_as_unknown(tmp_path):
    (tmp_path / "loadavg").write_text("0.50 0.40 0.30 1/100 42\n")
    (tmp_path / "meminfo").write_text("MemTotal: 16000000 kB\n")

    assert host_budget.read_host(tmp_path) is None


def test_a_readable_host_gives_its_sample(tmp_path):
    (tmp_path / "loadavg").write_text("0.50 0.40 0.30 1/100 42\n")
    (tmp_path / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 2048000 kB\n")

    sample = host_budget.read_host(tmp_path)

    assert (sample.load1, sample.available_mb, sample.agents) == (0.5, 2000, 0)
