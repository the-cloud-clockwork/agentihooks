import contextlib
import io
import sys
import unittest

from scripts.swarm_ledger import run


def help_of(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), self_exit() as code:
        run([*argv, "--help"])
    return code[0], out.getvalue()


@contextlib.contextmanager
def self_exit():
    code = [None]
    try:
        yield code
    except SystemExit as exc:
        code[0] = exc.code


class Entry(unittest.TestCase):
    def test_bare_command_runs_the_agent_cli(self):
        code, text = help_of([])
        self.assertEqual(code, 0)
        self.assertIn("agentihooks ledger", text)
        self.assertIn("priority", text)

    def test_tool_words_run_their_tool(self):
        for word, marker in (
            ("new", "agentihooks ledger new"),
            ("serve", "--ensure"),
            ("watch", "--as"),
            ("chat", "--as"),
        ):
            with self.subTest(word=word):
                code, text = help_of([word])
                self.assertEqual(code, 0)
                self.assertIn(marker, text)

    def test_the_caller_keeps_its_argv(self):
        before = list(sys.argv)
        help_of(["watch"])
        self.assertEqual(sys.argv, before)

    def test_the_join_paragraph_names_the_agentihooks_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run(["--slug", "demo-2026-01-01", "--as", "a1", "prompt"])
        self.assertIn("agentihooks ledger --slug demo-2026-01-01 --as a1 join", out.getvalue())
        self.assertIn("agentihooks ledger watch demo-2026-01-01 --as a1", out.getvalue())
        self.assertNotIn("python3", out.getvalue())
