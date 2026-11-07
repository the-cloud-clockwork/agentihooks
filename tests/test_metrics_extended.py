"""Extended tests for hooks.observability.metrics module."""

import json
import time

import pytest

pytestmark = pytest.mark.unit


class TestTimerExtended:
    """Extended Timer tests."""

    def test_manual_start_stop(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        t.start()
        time.sleep(0.01)
        t.stop()
        assert t.elapsed_ms > 0

    def test_elapsed_before_start(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        assert t.elapsed_ms == 0.0

    def test_elapsed_s(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        t.start()
        time.sleep(0.01)
        t.stop()
        assert t.elapsed_s > 0
        assert t.elapsed_s == t.elapsed_ms / 1000

    def test_reset(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        t.start()
        time.sleep(0.01)
        t.stop()
        assert t.elapsed_ms > 0
        t.reset()
        assert t.elapsed_ms == 0.0

    def test_running_timer_elapsed(self):
        """elapsed_ms returns current time while timer is still running.

        Don't stop — should still report elapsed time.
        """
        from hooks.observability.metrics import Timer

        t = Timer()
        t.start()
        time.sleep(0.01)
        assert t.elapsed_ms > 0

    def test_start_returns_self(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        result = t.start()
        assert result is t

    def test_stop_returns_self(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        t.start()
        result = t.stop()
        assert result is t

    def test_reset_returns_self(self):
        from hooks.observability.metrics import Timer

        t = Timer()
        result = t.reset()
        assert result is t


class TestMetricsCollectorExtended:
    """Extended MetricsCollector tests."""

    def test_record_creates_metric(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        m = c.record("op1", 42.5)
        assert m.name == "op1"
        assert m.elapsed_ms == 42.5
        assert m.success is True
        assert len(c.metrics) == 1

    def test_record_with_failure(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        m = c.record("op1", 100.0, success=False, error="timeout")
        assert m.success is False
        assert m.error == "timeout"

    def test_record_with_metadata(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        m = c.record("op1", 10.0, metadata={"tool": "CLI"})
        assert m.metadata["tool"] == "CLI"

    def test_measure_context_manager(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        with c.measure("op1", {"env": "test"}):
            time.sleep(0.01)
        assert len(c.metrics) == 1
        assert c.metrics[0].name == "op1"
        assert c.metrics[0].success is True
        assert c.metrics[0].elapsed_ms > 0

    def test_measure_with_exception(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        with pytest.raises(ValueError):
            with c.measure("failing_op"):
                raise ValueError("boom")
        assert len(c.metrics) == 1
        assert c.metrics[0].success is False
        assert c.metrics[0].error == "boom"

    def test_start_end_cycle(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.start("step1")
        time.sleep(0.01)
        m = c.end(success=True, metadata={"step": 1})
        assert m.name == "step1"
        assert m.elapsed_ms > 0
        assert m.metadata["step"] == 1

    def test_start_end_failure(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.start("step1")
        m = c.end(success=False, error="fail reason")
        assert m.success is False
        assert m.error == "fail reason"

    def test_get_failures(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("ok", 10.0, success=True)
        c.record("bad", 20.0, success=False, error="err")
        c.record("ok2", 30.0, success=True)
        failures = c.get_failures()
        assert len(failures) == 1
        assert failures[0].name == "bad"

    def test_get_by_metadata(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("a", 10.0, metadata={"tool": "CLI"})
        c.record("b", 20.0, metadata={"tool": "API"})
        c.record("c", 30.0, metadata={"tool": "CLI"})
        cli_metrics = c.get_by_metadata("tool", "CLI")
        assert len(cli_metrics) == 2

    def test_clear(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("a", 10.0)
        c.record("b", 20.0)
        c.clear()
        assert len(c.metrics) == 0

    def test_to_dict(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("my_metrics")
        c.record("op", 50.0)
        d = c.to_dict()
        assert d["name"] == "my_metrics"
        assert "summary" in d
        assert "metrics" in d
        assert len(d["metrics"]) == 1


class TestMetricsSummary:
    """Test MetricsSummary statistics."""

    def test_summary_empty(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        s = c.summary()
        assert s.count == 0
        assert s.avg_ms == 0.0
        assert s.success_rate == 0.0

    def test_summary_statistics(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        for ms in [10.0, 20.0, 30.0, 40.0, 50.0]:
            c.record("op", ms)
        s = c.summary()
        assert s.count == 5
        assert s.avg_ms == 30.0
        assert s.min_ms == 10.0
        assert s.max_ms == 50.0
        assert s.median_ms == 30.0
        assert s.total_ms == 150.0
        assert s.success_rate == 100.0

    def test_summary_p95_p99(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        for i in range(100):
            c.record("op", float(i + 1))
        s = c.summary()
        assert s.p95_ms >= 95.0
        assert s.p99_ms >= 99.0

    def test_summary_with_failures(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("a", 10.0, success=True)
        c.record("b", 20.0, success=False, error="err")
        s = c.summary()
        assert s.success_count == 1
        assert s.failure_count == 1
        assert s.success_rate == 50.0

    def test_summary_to_dict(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("op", 10.0)
        s = c.summary()
        d = s.to_dict()
        assert "count" in d
        assert "avg_ms" in d
        assert "p95_ms" in d
        assert "p99_ms" in d

    def test_summary_metadata_counts(self):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("a", 10.0, metadata={"env": "prod"})
        c.record("b", 20.0, metadata={"env": "prod"})
        c.record("c", 30.0, metadata={"env": "dev"})
        s = c.summary()
        assert s.metadata_counts["env"]["prod"] == 2
        assert s.metadata_counts["env"]["dev"] == 1


class TestSaveLoad:
    """Test save/load JSON persistence."""

    def test_save_and_load(self, tmp_path):
        """Load."""
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test_save")
        c.record("op1", 10.0, metadata={"tool": "CLI"})
        c.record("op2", 20.0, success=False, error="err")

        filepath = tmp_path / "metrics.json"
        c.save(str(filepath))

        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert data["name"] == "test_save"
        assert len(data["metrics"]) == 2

        loaded = MetricsCollector.load(str(filepath))
        assert loaded.name == "test_save"
        assert len(loaded.metrics) == 2
        assert loaded.metrics[0].name == "op1"
        assert loaded.metrics[1].success is False

    def test_save_creates_parent_dirs(self, tmp_path):
        from hooks.observability.metrics import MetricsCollector

        c = MetricsCollector("test")
        c.record("op", 10.0)
        filepath = tmp_path / "deep" / "nested" / "metrics.json"
        c.save(str(filepath))
        assert filepath.exists()


class TestResultAccumulatorExtended:
    """Extended ResultAccumulator tests."""

    def test_add_timeout(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator("test")
        acc.add_timeout("task_1")
        assert acc.timeout_count == 1
        assert acc.total_count == 1

    def test_add_skip(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator("test")
        acc.add_skip("task_1", reason="not applicable")
        assert acc.total_count == 1

    def test_get_failures(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator()
        acc.add_success("t1")
        acc.add_failure("t2", error="err")
        failures = acc.get_failures()
        assert len(failures) == 1
        assert failures[0]["task_id"] == "t2"

    def test_get_successes(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator()
        acc.add_success("t1", data={"result": 42})
        acc.add_failure("t2", error="err")
        successes = acc.get_successes()
        assert len(successes) == 1
        assert successes[0]["data"]["result"] == 42

    def test_summary_dict(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator("my_results")
        acc.add_success("t1")
        acc.add_failure("t2", error="err")
        s = acc.summary()
        assert s["name"] == "my_results"
        assert s["total"] == 2
        assert s["success"] == 1
        assert s["failure"] == 1
        assert s["success_rate"] == 50.0

    def test_to_dict(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator()
        acc.add_success("t1")
        d = acc.to_dict()
        assert "summary" in d
        assert "results" in d
        assert len(d["results"]) == 1

    def test_save(self, tmp_path):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator("test")
        acc.add_success("t1")
        filepath = tmp_path / "results.json"
        acc.save(str(filepath))
        assert filepath.exists()
        data = json.loads(filepath.read_text())
        assert data["summary"]["total"] == 1

    def test_elapsed_s(self):
        from hooks.observability.metrics import ResultAccumulator

        acc = ResultAccumulator()
        time.sleep(0.01)
        assert acc.elapsed_s > 0


class TestMetricDataclass:
    """Test the Metric dataclass."""

    def test_metric_to_dict(self):
        from hooks.observability.metrics import Metric

        m = Metric(
            name="op1",
            timestamp="2026-01-01T00:00:00Z",
            elapsed_ms=42.5,
            success=True,
            error=None,
            metadata={"key": "val"},
        )
        d = m.to_dict()
        assert d["name"] == "op1"
        assert d["elapsed_ms"] == 42.5
        assert d["metadata"]["key"] == "val"

    def test_metric_defaults(self):
        from hooks.observability.metrics import Metric

        m = Metric(name="op", timestamp="now", elapsed_ms=0.0)
        assert m.success is True
        assert m.error is None
        assert m.metadata == {}


class TestTimedDecorator:
    """Test the @timed decorator."""

    def test_timed_preserves_return(self):
        from hooks.observability.metrics import timed

        @timed
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_timed_preserves_name(self):
        from hooks.observability.metrics import timed

        @timed
        def my_func():
            pass

        assert my_func.__name__ == "my_func"

    def test_timed_with_kwargs(self):
        from hooks.observability.metrics import timed

        @timed
        def greet(name="world"):
            return f"hello {name}"

        assert greet(name="test") == "hello test"
