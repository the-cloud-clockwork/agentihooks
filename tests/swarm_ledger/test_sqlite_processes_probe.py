import json
import multiprocessing
import os
import threading
import traceback

import pytest

from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger.test_sqlite import CONTENT


def join(path, index, log):
    descriptor = os.open(f"{log}.err", os.O_WRONLY | os.O_CREAT, 0o600)
    os.dup2(descriptor, 2)
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


def busy(path, same, stop):
    repo = SQLiteLedgerRepository(path if same else path.parent.parent / "other" / sqlite.DATABASE)
    if not same:
        repo.create("other", CONTENT)
    turn = 0
    while not stop.is_set():
        turn += 1
        if same:
            repo.get_document("processes")
            repo.exists("processes")
        else:
            repo.apply_ops("other", ops=[{"op": "add", "id": f"o{turn}", "thread": "chat", "text": "x"}])


@pytest.mark.parametrize("method", ["fork", "spawn"])
@pytest.mark.parametrize("background", ["other", "same"])
@pytest.mark.parametrize("case", range(2))
def test_probe(tmp_path, case, background, method):
    failures = []
    for attempt in range(30):
        base = tmp_path / str(attempt)
        path = base / "ledgers" / sqlite.DATABASE
        SQLiteLedgerRepository(path).create("processes", CONTENT)
        stop = threading.Event()
        thread = threading.Thread(target=busy, args=(path, background == "same", stop), daemon=True)
        thread.start()
        context = multiprocessing.get_context(method)
        logs = [base / f"child{index}.log" for index in range(4)]
        workers = [context.Process(target=join, args=(path, index, logs[index])) for index in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        stop.set()
        thread.join(30)
        codes = [worker.exitcode for worker in workers]
        for worker in workers:
            if worker.exitcode is None:
                worker.kill()
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
                    "logs": [log.read_text()[-3000:] if log.exists() else None for log in logs],
                    "errs": [
                        open(f"{log}.err").read()[-3000:] if os.path.exists(f"{log}.err") else None for log in logs
                    ],
                }
            )
    print(f"PROBE method={method} background={background} case={case} failures={len(failures)}")
    for failure in failures[:3]:
        print("PROBE", json.dumps(failure))
    assert not failures
