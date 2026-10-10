from dataclasses import asdict

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.runtime.operations import Phase
from tests.test_swarm_v2_controller import agent, fixture, request


def _process_admission(address, barrier, outcomes):
    from redis import Redis

    store = RedisStore(Redis(host=address[0], port=address[1], decode_responses=True, protocol=3, socket_timeout=2))
    lease.now_ms = lambda store: 1000
    controller = Controller(store, "fixture", [], lambda: True)
    barrier.wait(timeout=10)
    won = controller.acquire()
    if won:
        candidate = agent(store)
        controller.admit(candidate)
    outcomes.put(won)


def process_race():
    import multiprocessing
    from threading import Thread

    from fakeredis import TcpFakeServer
    from redis import Redis

    server = TcpFakeServer(("127.0.0.1", 0))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Redis(
        host=server.server_address[0],
        port=server.server_address[1],
        decode_responses=True,
        protocol=3,
        socket_timeout=2,
    )
    store = RedisStore(client)
    context = multiprocessing.get_context("spawn")
    barrier, outcomes = context.Barrier(2), context.Queue()
    processes = [
        context.Process(target=_process_admission, args=(server.server_address, barrier, outcomes)) for _ in range(2)
    ]
    try:
        store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        winners = [outcomes.get(timeout=2) for _ in processes]
        records = store.execution_registry.records("fixture")
        intents = store.redis.hlen(store.key("fixture", "controller-intents"))
        changes = int(store.redis.get(store.key("fixture", "controller-leader-changes")) or 0)
        return {
            "passed": sum(winners) == len(records) == intents == changes == 1,
            "controller_processes": len(processes),
            "admitted_generations": len(records),
            "controller_leader_changes_total": changes,
            "intents": intents,
        }
    finally:
        for process in processes:
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            process.close()
        outcomes.close()
        outcomes.join_thread()
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def case_a():
    runs = [process_race() for _ in range(2)]
    return {"passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def case_b():
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        store, (former, replacement), transport, clock, _ = fixture.__wrapped__(patch)
        assert former.acquire()
        attempt = former.admit(agent(store))
        candidate = agent(store)
        clock[0] += lease.ttl_ms()
        assert replacement.acquire()
        assert former.ready and former.held is not None
        keys = [store.key("fixture", kind) for kind in ("executions", "controller-intents", "runtime-operations")]
        before = [store.redis.hgetall(key) for key in keys]
        admission_refused = False
        try:
            former.admit(candidate)
        except SwarmError as error:
            assert str(error) == "the controller lease is stale"
            admission_refused = True
        refused = False
        try:
            former.execute(request(attempt))
        except SwarmError as error:
            assert str(error) == "the controller lease is stale"
            refused = True
        assert not former.renew()
        return {
            "passed": admission_refused
            and refused
            and transport.creations == 0
            and before == [store.redis.hgetall(key) for key in keys],
            "cached_admission_refused": admission_refused,
            "former_leader_refused": refused,
            "protected_state_unchanged": before == [store.redis.hgetall(key) for key in keys],
            "external_creations": transport.creations,
            "controller_leader_changes_total": replacement.controller_leader_changes_total(),
        }


def case_c():
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        store, (former, replacement), transport, clock, grant = fixture.__wrapped__(patch)
        assert former.acquire()
        attempt = former.admit(agent(store))
        transport.lose_ack = True
        interrupted = former.execute(request(attempt))
        assert interrupted.phase is Phase.UNKNOWN
        clock[0] += lease.ttl_ms()
        assert replacement.acquire()
        recovered = store.operation_journal.get("fixture", interrupted.operation_id)
        replay = replacement.execute(request(attempt))
        assert replacement.release()
        rollback = Controller(store, "fixture", [transport], lambda: grant["allowed"], admission_enabled=False)
        assert rollback.acquire()
        refused = False
        try:
            rollback.admit(agent(store))
        except SwarmError:
            refused = True
        return {
            "passed": recovered == replay and recovered.phase is Phase.APPLIED and transport.creations == 1 and refused,
            "confirmed_operation": asdict(recovered),
            "external_creations": transport.creations,
            "rollback_admission_refused": refused,
            "retained_intents": rollback.intents(),
            "controller_leader_changes_total": rollback.controller_leader_changes_total(),
        }
