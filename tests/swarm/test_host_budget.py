import pytest

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
    assert "MB available memory less 0 of 0 MB swap in use fits 12" in decision.reason


def test_each_room_names_the_limit_that_set_it():
    assert host_budget.room(_sample(load1=40.0, available_mb=16000, agents=10), LIMITS, previous=5).limit == "load"
    assert host_budget.room(_sample(load1=24.0, available_mb=16000, agents=10), LIMITS, previous=3).limit == "load"
    assert host_budget.room(_sample(load1=24.0, available_mb=1400, agents=10), LIMITS, previous=5).limit == "memory"
    assert host_budget.room(_sample(load1=18.0, available_mb=16000, agents=18), LIMITS).limit == "load"
    assert host_budget.room(_sample(load1=0.0, available_mb=2100), LIMITS).limit == "memory"


def _swapped(available_mb: int, swap_used_mb: int, swap_total_mb: int, agent_mb: int = 0, measured: int = 0):
    return HostSample(
        load1=0.0,
        cpus=20,
        available_mb=available_mb,
        agents=0,
        swap_used_mb=swap_used_mb,
        swap_total_mb=swap_total_mb,
        agent_mb=agent_mb,
        measured_agents=measured,
    )


def test_swap_in_use_comes_off_available_memory():
    assert host_budget.memory_room(_swapped(7000, 2800, 8000), LIMITS) == 6
    assert host_budget.memory_room(_swapped(300, 600, 10000), LIMITS) == 0


def test_no_agent_roots_skip_the_process_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(host_budget, "_children", lambda proc: pytest.fail("scanned processes without agent roots"))

    assert host_budget.agent_memory(tmp_path, {}) == (0, 0)


def test_memory_room_is_zero_above_the_swap_use_share_while_available_memory_is_below_the_floor():
    assert host_budget.memory_room(_swapped(4095, 950, 1000), LIMITS) == 0
    assert host_budget.memory_room(_swapped(4096, 950, 1000), LIMITS) == 5
    assert host_budget.memory_room(_swapped(4095, 900, 1000), LIMITS) == 4
    assert host_budget.memory_room(_swapped(16000, 0, 0), LIMITS) == 22


def test_stale_swap_above_the_share_does_not_cut_room_from_available_memory():
    assert host_budget.memory_room(_swapped(9591, 3716, 4096), LIMITS) == 13
    assert host_budget.memory_room(_swapped(16000, 3700, 4096), LIMITS) == 22
    assert host_budget.memory_room(_swapped(4197, 4058, 4096), LIMITS) == 5
    assert host_budget.memory_room(_swapped(4096, 4058, 4096), LIMITS) == 5


def test_full_swap_still_cuts_room_and_swap_below_the_share_still_comes_off():
    assert host_budget.memory_room(_swapped(4095, 4058, 4096), LIMITS) == 0
    assert host_budget.memory_room(_swapped(4095, 3686, 4096), LIMITS) == 0
    assert host_budget.memory_room(_swapped(16000, 3686, 4096), LIMITS) == 17


def test_the_swap_use_share_and_floor_are_configured():
    share = Thresholds(swap_full_share=0.5, swap_floor_mb=2000)

    assert host_budget.memory_room(_swapped(1999, 501, 1000), share) == 0
    assert host_budget.memory_room(_swapped(2100, 501, 1000), share) == 3
    assert host_budget.memory_room(_swapped(2100, 500, 1000), share) == 2
    assert Thresholds().swap_full_share == host_budget.SWAP_FULL_SHARE == 0.9
    assert Thresholds().swap_floor_mb == host_budget.SWAP_FLOOR_MB == 4096
    configured = host_budget.thresholds(SwarmConfig("sw", "/repo", 1, 1, swap_full_share=0.6, swap_floor_mb=3000))
    assert (configured.swap_full_share, configured.swap_floor_mb) == (0.6, 3000)


def test_measured_agent_memory_sets_the_per_agent_figure():
    assert host_budget.memory_room(_swapped(7000, 0, 4096, agent_mb=1400, measured=3), LIMITS) == 5
    assert host_budget.memory_room(_swapped(7000, 0, 4096), LIMITS) == 10


def test_the_configured_figure_is_the_floor_under_a_lighter_measured_agent():
    assert host_budget.memory_room(_swapped(7000, 0, 4096, agent_mb=433, measured=20), LIMITS) == 10
    assert host_budget.per_agent_mb(_swapped(7000, 0, 4096, agent_mb=701, measured=1), LIMITS) == 701


def test_host_room_line_names_swap_and_the_per_agent_figure():
    measured = host_budget.room(_swapped(7000, 1400, 8000, agent_mb=1400, measured=3), LIMITS)
    configured = host_budget.room(_swapped(7000, 1400, 8000), LIMITS)
    full = host_budget.room(_swapped(4000, 950, 1000), LIMITS)

    assert measured.reason == (
        "one minute load 0.00 per CPU is below the low watermark; 7000 MB available memory less 1400 of 8000 MB "
        "swap in use fits 4 at 1400 MB each, the larger of 1400 MB measured over 3 Claude agent trees and the "
        "configured 700 MB"
    )
    assert configured.reason.endswith("fits 8 at 700 MB each, the configured figure")
    assert (full.room, full.limit) == (0, "memory")
    assert full.reason.endswith(
        "swap 950 of 1000 MB in use is above the 90% share and 4000 MB available memory is below the 4096 MB "
        "floor, no memory room"
    )


def test_host_room_line_names_stale_swap_above_the_share():
    stale = host_budget.room(_swapped(9591, 3716, 4096), LIMITS)

    assert (stale.room, stale.limit) == (13, "memory")
    assert stale.reason == (
        "one minute load 0.00 per CPU is below the low watermark; swap 3716 of 4096 MB in use is above the 90% "
        "share but 9591 MB available memory is at or above the 4096 MB floor, so the swap reads stale; 9591 MB "
        "available memory fits 13 at 700 MB each, the configured figure"
    )


def _process(proc, pid: int, ppid: int, rss_kb: int | None) -> None:
    folder = proc / str(pid)
    folder.mkdir()
    (folder / "stat").write_text(f"{pid} (node (x)) S {ppid} 1 1 0\n")
    status = "Name:\tnode\n" + (f"VmRSS:\t  {rss_kb} kB\n" if rss_kb is not None else "")
    (folder / "status").write_text(status)


def test_read_host_reads_swap_and_measures_live_agent_process_trees(tmp_path, monkeypatch):
    (tmp_path / "loadavg").write_text("2.00 1.00 1.00 3/900 4242\n")
    (tmp_path / "meminfo").write_text(
        "MemAvailable:    5120900 kB\nDirectMap4k:     unknown\nSwapTotal:       4194904 kB\nSwapFree:          98404 kB\n"
    )
    for pid, ppid, rss in ((10, 1, 300000), (11, 10, 200000), (12, 11, 100000), (20, 1, 1652799), (30, 1, 999999)):
        _process(tmp_path, pid, ppid, rss)
    _process(tmp_path, 13, 10, None)
    _process(tmp_path, 50, 1, None)
    monkeypatch.setattr(host_budget.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        host_budget.account_sessions, "live_sessions", lambda proc: {40: "c", 10: "a", 20: "b", 50: "d"}
    )
    monkeypatch.setattr(host_budget.account_sessions, "live_codex_sessions", lambda proc: 1)

    sample = host_budget.read_host(tmp_path)

    assert {type(value) for value in (sample.available_mb, sample.swap_used_mb, sample.agent_mb)} == {int}
    assert sample == HostSample(
        load1=2.0,
        cpus=4,
        available_mb=5000,
        agents=5,
        swap_used_mb=4000,
        swap_total_mb=4096,
        agent_mb=1099,
        measured_agents=2,
    )


def test_read_host_without_swap_lines_reads_no_swap_and_no_measured_agents(tmp_path, monkeypatch):
    (tmp_path / "loadavg").write_text("1.00 1.00 1.00 3/900 4242\n")
    (tmp_path / "meminfo").write_text("MemAvailable:    1024000 kB\n")
    monkeypatch.setattr(host_budget.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(host_budget.account_sessions, "live_sessions", lambda proc: {})
    monkeypatch.setattr(host_budget.account_sessions, "live_codex_sessions", lambda proc: 0)

    sample = host_budget.read_host(tmp_path)

    assert (sample.available_mb, sample.swap_used_mb, sample.swap_total_mb, sample.agent_mb) == (1000, 0, 0, 0)
    assert sample.measured_agents == 0


def test_read_host_without_available_memory_reads_nothing(tmp_path, monkeypatch):
    (tmp_path / "loadavg").write_text("1.00 1.00 1.00 3/900 4242\n")
    (tmp_path / "meminfo").write_text("SwapTotal:       4194304 kB\nSwapFree:       4194304 kB\n")
    monkeypatch.setattr(host_budget.account_sessions, "live_sessions", lambda proc: {})
    monkeypatch.setattr(host_budget.account_sessions, "live_codex_sessions", lambda proc: 0)

    assert host_budget.read_host(tmp_path) is None


def test_meminfo_values_are_read_after_the_first_colon(tmp_path):
    (tmp_path / "meminfo").write_text("MemAvailable:    2048 kB: x\nSwapTotal:  1024 kB\nSwapFree:  0 kB\n")

    assert host_budget._memory_mb(tmp_path) == (2, 1, 1)


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


def test_spawn_room_passes_a_room_its_reading_already_sized_through_unchanged():
    sized = host_budget.Room(3, "worker pool: sized elsewhere", "memory")

    assert host_budget.spawn_room(SwarmConfig("sw", "/repo", 1, 0), sized, 9) is sized


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
