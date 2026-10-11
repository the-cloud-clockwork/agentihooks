import pytest

from scripts import openbao_kv, openbao_step
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}


class Prompts:
    def __init__(self, answers=(), tty=True, hidden="typed-bao"):
        self.answers, self.prompts, self.is_tty, self.secret = list(answers), [], tty, hidden

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def hidden(self, prompt):
        self.prompts.append(prompt)
        return self.secret

    def tty(self):
        return self.is_tty


def _target(choice, environ, prompts):
    console = openbao_step.Console(prompts.ask, prompts.hidden, prompts.tty)
    return openbao_step.target(choice, environ, console, "opener")


@pytest.mark.parametrize(
    "choice, environ, answers, expected, prompts",
    [
        (False, BAO_ENV, [], None, []),
        (True, BAO_ENV, [], (BAO_URL, BAO_TOKEN), []),
        (None, BAO_ENV, ["N"], None, ["Do you want to store them in OpenBao? [y/N] "]),
        (None, BAO_ENV, [""], None, ["Do you want to store them in OpenBao? [y/N] "]),
        (
            None,
            BAO_ENV,
            ["Yes", ""],
            (BAO_URL, BAO_TOKEN),
            [
                "Do you want to store them in OpenBao? [y/N] ",
                f"OpenBao credentials detected ({BAO_URL}). Use them? [Y/n] ",
            ],
        ),
        (
            None,
            {},
            ["y", " https://typed.test "],
            ("https://typed.test", "typed-bao"),
            ["Do you want to store them in OpenBao? [y/N] ", "OpenBao URL: ", "OpenBao token: "],
        ),
        (True, {}, ["https://typed.test"], ("https://typed.test", "typed-bao"), ["OpenBao URL: ", "OpenBao token: "]),
    ],
)
def test_the_target_follows_the_flag_the_terminal_and_the_answers(choice, environ, answers, expected, prompts):
    console = Prompts(answers)
    found = _target(choice, environ, console)
    assert (None if found is None else (found.url, found.token)) == expected
    assert found is None or found.opener == "opener"
    assert console.prompts == prompts


def test_off_a_terminal_without_a_flag_the_step_is_skipped(capsys):
    assert _target(None, BAO_ENV, Prompts(tty=False)) is None
    assert capsys.readouterr().out == "not a terminal: skipping OpenBao (pass --openbao to store the tokens there)\n"


@pytest.mark.parametrize(
    "tty, hidden, message",
    [
        (False, "x", "--openbao needs OPENBAO_URL and OPENBAO_TOKEN off a terminal"),
        (True, "", "OpenBao needs both a URL and a token"),
    ],
)
def test_a_flagged_store_without_credentials_is_refused(tty, hidden, message):
    with pytest.raises(openbao_step.StoreRefused, match=message):
        _target(True, {}, Prompts(["https://typed.test"], tty=tty, hidden=hidden))


def test_typed_credentials_write_through_the_default_opener(monkeypatch):
    fake = FakeBao()
    monkeypatch.setattr(openbao_kv.urllib.request, "urlopen", fake)
    prompts = Prompts(["y", BAO_URL], hidden=BAO_TOKEN)
    bao = openbao_step.target(None, {}, openbao_step.Console(prompts.ask, prompts.hidden, prompts.tty), None)
    assert openbao_step.store(bao, openbao_kv.CODEX_CREDS, {"A": "1"}, ()) == 0
    assert fake.data == {"A": "1"}


def test_a_store_reports_the_written_keys_or_the_failure(capsys):
    bao = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, FakeBao({"OLD": "o", "KEEP": "k"}))
    assert openbao_step.store(bao, openbao_kv.CODEX_CREDS, {"A": "1"}, ("OLD",)) == 0
    sealed = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, FakeBao({"KEEP": "k"}, fail_writes=True))
    assert openbao_step.store(sealed, openbao_kv.CODEX_CREDS, {"A": "1"}, ()) == 1
    assert capsys.readouterr().out == (
        "OpenBao secret/agentihooks/codex-creds: wrote A (2 keys total)\n"
        "OpenBao write failed: OpenBao HTTP 503: sealed\n"
    )
