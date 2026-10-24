from scripts.swarm import recovery_view as rv
from tests.swarm_ledger import test_swarm_layout as layout
from tests.swarm_ledger.test_swarm_layout import status

browser = layout.browser
open_page = layout.open_page

SEAT = "eng-3@rig"


def card(**changes):
    material = {
        "execution_id": "exe-3",
        "generation": 1,
        "agent": "eng-7",
        "seat": SEAT,
        "task": "pb10",
        "reason": "node_lost",
        "fenced_at_ms": layout.NOW_MS - 120_000,
        "archive_watermark": 4096,
        "tail": "tail after 4096 is lost",
        "checkpoint": {"checkpoint_id": "ckpt-context-1", "sequence": 1, "continuation": "context"},
        "refused": [{"checkpoint_id": "ckpt-native-2", "sequence": 2, "diagnosis": "corrupt_object"}],
        "choices": {"native": "no complete compatible native checkpoint to resume from", "context": "", "fresh": ""},
        "decision": {"mode": rv.PENDING, "replacement": ""},
        "decided_at_ms": 0,
        "requested": "",
        "refused_request": None,
    }
    return rv.pod_card({**material, **changes}, layout.NOW_MS)


def payload(*cards):
    handoffs = [
        {"seat": SEAT, "at": 0, "reason": "", "continuity": "", "binding": "awaiting decision", "successor": "eng-7"}
        | {"awaiting": "eng-7"}
    ]
    return status(recovery={"cards": list(cards), rv.METRIC: {}}, handoffs=handoffs)


def buttons(page):
    return page.tab.locator(f'[id="recovery-{SEAT}"] button')


def test_the_recovery_row_shows_material_diagnosis_and_three_choices(open_page):
    page = open_page(payload(card()))
    row = page.table("swarm-recovery")[0]
    assert row[:3] == ["eng 3", "node lost", "CONTEXT ONLY"]
    assert "ckpt-context-1 · sequence 1 · context" in row[3] and "archive watermark 4096" in row[3]
    assert "passed over ckpt-native-2: corrupt_object" in row[4]
    assert "native resume: no complete compatible native checkpoint to resume from" in row[4]
    assert row[5] == "2m"
    assert buttons(page).all_inner_texts() == ["native resume", "context continuation", "fresh start"]
    assert [buttons(page).nth(i).is_disabled() for i in range(3)] == [True, False, False]
    assert buttons(page).first.get_attribute("title") == "no complete compatible native checkpoint to resume from"
    assert page.text("#recovery-count").lower() == "1 seat"


def test_a_choice_is_sent_as_a_restore_decision_and_the_handoff_row_points_to_recovery(open_page):
    page = open_page(payload(card()))
    assert page.tab.locator("#swarm-handoffs a", has_text="recovery").get_attribute("href") == f"#recovery-{SEAT}"
    assert page.tab.locator("#swarm-handoffs button").count() == 0
    buttons(page).nth(1).click()
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")
    assert page.puts == [{"action": "restore-decision", "agent": "eng-7", "choice": "context"}]


def test_an_accepted_or_requested_decision_replaces_the_choices(open_page):
    decision = {"mode": "context", "replacement": "exe-4"}
    page = open_page(payload(card(decision=decision, decided_at_ms=layout.NOW_MS)))
    assert buttons(page).count() == 0
    assert "ACCEPTED: CONTEXT CONTINUATION" in page.text("#swarm-recovery")
    assert "exe-4" in page.text("#swarm-recovery")
    page = open_page(payload(card(requested="fresh")))
    assert buttons(page).count() == 0
    assert "REQUESTED: FRESH START" in page.text("#swarm-recovery")


def test_a_path_only_card_is_never_green_and_without_cards_the_table_says_so(open_page):
    path_only = {**card(), "badge": rv.PATH_ONLY, "backend": "local"}
    page = open_page(payload(path_only))
    badge = page.tab.locator("#swarm-recovery .lbl").first
    assert badge.inner_text() == "PATH ONLY"
    assert "recoverable" not in (badge.get_attribute("class") or "")
    page = open_page(status())
    assert page.table("swarm-recovery") == [["No seat is waiting on a recovery decision."]]
    assert page.text("#recovery-count").lower() == "none waiting"


def test_rollback_without_recovery_material_leaves_the_seat_waiting_with_its_handoff_choices(open_page):
    page = open_page(payload())
    assert page.table("swarm-handoffs")[0][4].lower() == "awaiting decision"
    assert page.tab.locator("#swarm-handoffs button").all_inner_texts() == ["resume", "fresh"]
    page.tab.locator("#swarm-handoffs button", has_text="fresh").click()
    page.tab.wait_for_function("() => !document.querySelector('#swarm-modes button').disabled")
    assert page.puts == [{"action": "restore-decision", "agent": "eng-7", "choice": "fresh"}]
