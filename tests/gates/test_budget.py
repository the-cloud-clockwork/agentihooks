import threading

from scripts.gates.budget import Budget


def test_spend_counts_up_to_the_cap_then_refuses_without_counting(tmp_path):
    budget = Budget("demo", "subagents", tmp_path)
    assert [budget.spend("t1", "launches", 2) for _ in range(3)] == [(True, 1), (True, 2), (False, 2)]
    assert budget.spent("t1", "launches") == 2


def test_each_subject_and_counter_keeps_its_own_count(tmp_path):
    budget = Budget("demo", "subagents", tmp_path)
    budget.spend("t1", "launches", 5)
    budget.spend("t1", "launches", 5)
    budget.spend("t1", "continuations", 5)
    budget.spend("t2", "launches", 5)
    assert (budget.spent("t1", "launches"), budget.spent("t1", "continuations")) == (2, 1)
    assert (budget.spent("t2", "launches"), budget.spent("t3", "launches")) == (1, 0)
    assert Budget("demo", "reruns", tmp_path).spent("t1", "launches") == 0
    assert Budget("other", "subagents", tmp_path).spent("t1", "launches") == 0


def test_counts_live_under_the_gates_folder_with_safe_names(tmp_path):
    Budget("demo", "subagents", tmp_path).spend("../t1", "launches", 1)
    assert (tmp_path / "demo" / "gates" / "subagents" / "counts" / "___t1" / "launches").read_text() == "1"


def test_an_unreadable_count_reads_as_zero(tmp_path):
    budget = Budget("demo", "subagents", tmp_path)
    budget.path("t1", "launches").parent.mkdir(parents=True)
    budget.path("t1", "launches").write_text("garbage")
    assert budget.spent("t1", "launches") == 0
    assert budget.spend("t1", "launches", 3) == (True, 1)


def test_parallel_spends_never_pass_the_cap(tmp_path):
    budget, results = Budget("demo", "subagents", tmp_path), []

    def spend():
        results.append(budget.spend("t1", "launches", 5))

    threads = [threading.Thread(target=spend) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for allowed, _ in results if allowed) == 5
    assert budget.spent("t1", "launches") == 5
