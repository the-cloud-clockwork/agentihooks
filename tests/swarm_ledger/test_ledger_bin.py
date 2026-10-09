import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_bin  # noqa: E402
import ledger_core as core  # noqa: E402

from scripts.swarm_ledger.repository import bin_storage, repository


@pytest.fixture(autouse=True)
def empty_bin():
    with repository.connect() as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        repository.save_registry(connection, "bin", {})
        repository.save_registry(connection, "restored", {})


def test_a_closed_ledger_goes_to_the_bin_at_the_given_time():
    assert bin_storage.bin_closed("closed-one", closed_at=100, now=200) is True
    assert ledger_bin.entries() == {"closed-one": 200}


def test_a_ledger_already_in_the_bin_keeps_its_first_time():
    bin_storage.bin_closed("closed-one", closed_at=100, now=200)
    assert bin_storage.bin_closed("closed-one", closed_at=100, now=300) is False
    assert ledger_bin.entries() == {"closed-one": 200}


@pytest.mark.parametrize("restored_at", [100, 400])
def test_a_restore_at_or_after_the_close_keeps_the_ledger_out(restored_at):
    bin_storage.bin_closed("closed-one", closed_at=50, now=60)
    ledger_bin.restore("closed-one", now=restored_at)
    assert bin_storage.bin_closed("closed-one", closed_at=100, now=500) is (restored_at < 100)
    assert "closed-one" not in ledger_bin.entries()


def test_closing_again_after_a_restore_bins_it_again():
    bin_storage.bin_closed("closed-one", closed_at=50, now=60)
    ledger_bin.restore("closed-one", now=99)
    assert bin_storage.bin_closed("closed-one", closed_at=100, now=700) is True
    assert ledger_bin.entries() == {"closed-one": 700}


def test_a_restore_of_another_ledger_does_not_shield_this_one():
    bin_storage.bin_closed("other", closed_at=1, now=2)
    ledger_bin.restore("other", now=900)
    assert bin_storage.bin_closed("closed-one", closed_at=100, now=200) is True


def test_without_a_time_the_ledger_is_binned_now():
    before = core.now_ms()
    assert bin_storage.bin_closed("closed-one", closed_at=100) is True
    assert before <= ledger_bin.entries()["closed-one"] <= core.now_ms()
