import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

pytestmark = pytest.mark.unit


def _write(tmp_path, data):
    f = tmp_path / "usage.json"
    f.write_text(json.dumps(data))
    return str(f)


def _now_str(delta_sec=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestLoadQuota:
    def test_disabled(self):
        with patch.dict("os.environ", {"CLAUDE_USAGE_FILE": ""}):
            from importlib import reload
            import hooks.quota as q; reload(q)
            assert q.load_quota() is None

    def test_missing_file(self, tmp_path):
        with patch.dict("os.environ", {"CLAUDE_USAGE_FILE": str(tmp_path / "nope.json")}):
            import hooks.quota as q
            assert q.load_quota() is None

    def test_fresh(self, tmp_path):
        p = _write(tmp_path, {"_updated": _now_str(), "session": {"used_pct": 9}})
        with patch.dict("os.environ", {"CLAUDE_USAGE_FILE": p, "CLAUDE_USAGE_STALE_SEC": "300"}):
            import hooks.quota as q
            result = q.load_quota()
            assert result is not None
            assert result["session"]["used_pct"] == 9

    def test_stale(self, tmp_path):
        p = _write(tmp_path, {"_updated": _now_str(-400)})
        with patch.dict("os.environ", {"CLAUDE_USAGE_FILE": p, "CLAUDE_USAGE_STALE_SEC": "300"}):
            import hooks.quota as q
            assert q.load_quota() == {"stale": True}

    def test_bad_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        with patch.dict("os.environ", {"CLAUDE_USAGE_FILE": str(f)}):
            import hooks.quota as q
            assert q.load_quota() is None


class TestFmtQuota:
    def setup_method(self):
        import hooks.quota as q
        self.fmt = q.fmt_quota

    def test_stale(self):
        assert self.fmt({"stale": True}) == "stale"

    def test_full(self):
        data = {
            "session": {"used_pct": 9, "resets_in_sec": 3600},
            "weekly": {"all_models": {"used_pct": 29}},
            "monthly_spend": {"amount": 39, "limit": 100, "currency": "EUR", "used_pct": 39},
        }
        out = self.fmt(data)
        assert "s:9%" in out
        assert "w:29%" in out
        assert "€39/100" in out
        assert "[1h]" in out

    def test_no_spend(self):
        data = {"session": {"used_pct": 5}, "weekly": {}, "monthly_spend": None}
        out = self.fmt(data)
        assert "€" not in out and "$" not in out

    def test_resets_hidden_when_far(self):
        data = {"session": {"used_pct": 5, "resets_in_sec": 10800}}
        out = self.fmt(data)
        assert "[" not in out
