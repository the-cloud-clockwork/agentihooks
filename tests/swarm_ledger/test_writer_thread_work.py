import cProfile
import pstats
import threading
import types

from scripts.swarm_ledger.repository import entry, fragments
from tests.swarm_ledger import operations_cpu_replay as replay
from tests.swarm_ledger.test_operations_cpu_contract import small_corpus

WHOLE_TEXT = {(fragments.__file__, "encoded"), (entry.__file__, "text")}


def test_operations_writes_leave_the_stream_view_and_whole_ledger_encoding_off_the_writer_thread(tmp_path, monkeypatch):
    profiles, publishers = [], []

    class Capturing(cProfile.Profile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            profiles.append(self)

    publish = replay.server.publish_snapshot

    def recorded(slug, state):
        publishers.append(threading.current_thread())
        return publish(slug, state)

    monkeypatch.setattr(replay, "corpus", small_corpus)
    monkeypatch.setattr(replay, "cProfile", types.SimpleNamespace(Profile=Capturing))
    monkeypatch.setattr(replay.server, "publish_snapshot", recorded)
    report = replay.replay(tmp_path, monkeypatch, profiled=True)
    called = {(path, name) for path, _, name in pstats.Stats(profiles[0]).stats}
    assert report["outputs"][0]["reply"]["applied"] == ["accepted"]
    assert report["outputs"][1]["reply"]["rejected"] == ["missing"]
    assert report["outputs"][0]["stream"] != replay.fingerprint([])
    assert any(name == "apply_ops" for _, name in called)
    assert called & WHOLE_TEXT == set()
    assert publishers
    assert threading.current_thread() not in publishers
