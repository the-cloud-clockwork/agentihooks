import json
import multiprocessing
import os
import traceback

import pytest

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import CONTENT


def join(path, index, log):
    try:
        repo = SQLiteLedgerRepository(path)
        lines = []
        for turn in range(3):
            state, _ = repo.apply_ops("processes", ops=[{"op": "join", "id": f"j{index}-{turn}", "by": f"eng{index}"}])
            lines.append(["join", turn, state["_meta"]["rev"], sorted(state["_meta"]["members"])])
            state, _ = repo.apply_ops(
                "processes", ops=[{"op": "add", "id": f"m{index}-{turn}", "thread": "chat", "text": "hi"}]
            )
            lines.append(["add", turn, state["_meta"]["rev"], sorted(m["id"] for m in state["chat"])])
        log.write_text(json.dumps(lines))
    except BaseException:
        log.write_text(traceback.format_exc())
        os._exit(1)


@pytest.mark.parametrize("case", range(8))
def test_probe(tmp_path, case):
    failures = []
    for attempt in range(60):
        base = tmp_path / str(attempt)
        path = base / "ledgers" / sqlite.DATABASE
        SQLiteLedgerRepository(path).create("processes", CONTENT)
        context = multiprocessing.get_context("fork")
        logs = [base / f"child{index}.log" for index in range(4)]
        workers = [context.Process(target=join, args=(path, index, logs[index])) for index in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        codes = [worker.exitcode for worker in workers]
        state = SQLiteLedgerRepository(path).get_document("processes")
        members = sorted(state["_meta"]["members"])
        chat = sorted(m["id"] for m in state["chat"])
        if codes != [0] * 4 or len(members) != 4 or len(chat) != 12:
            failures.append(
                {
                    "attempt": attempt,
                    "codes": codes,
                    "members": members,
                    "chat": chat,
                    "rev": state["_meta"]["rev"],
                    "logs": [log.read_text() if log.exists() else None for log in logs],
                }
            )
    print(f"PROBE case={case} failures={len(failures)}")
    for failure in failures:
        print("PROBE", json.dumps(failure))
    assert not failures
