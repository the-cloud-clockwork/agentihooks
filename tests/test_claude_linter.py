"""Tests for scripts.claude_linter."""

import pytest

pytestmark = pytest.mark.unit


class TestClaudeLinter:
    """Tests for CLAUDE.md linting and skill extraction."""

    def test_estimate_tokens(self):
        from scripts.claude_linter import estimate_tokens

        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("") == 0

    def test_parse_sections(self, tmp_path):
        from scripts.claude_linter import PREAMBLE_HEADING, parse_sections

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Title\n\n## Commands\nRun this.\n\n## Architecture\nDesign.\n")

        sections = parse_sections(md)
        # The H1 title before the first H2 is reported as a preamble section so
        # per-section tokens reconcile with the whole-file total.
        assert [s.heading for s in sections] == [PREAMBLE_HEADING, "Commands", "Architecture"]

    def test_section_tokens_reconcile_with_total(self, tmp_path):
        """Per-section tokens must account for the whole file, preamble included."""
        from scripts.claude_linter import lint_report

        md = tmp_path / "CLAUDE.md"
        md.write_text(
            "# Title\n\nIntro prose before any heading.\n\n"
            "<!-- BEGIN injected -->\nblock content\n<!-- END injected -->\n\n"
            "## Commands\nRun this.\n"
        )
        report = lint_report(md)
        joined = "\n".join(s.content for s in report.sections)
        # Nothing before the first heading may be silently dropped.
        assert "Intro prose before any heading." in joined
        assert "block content" in joined
        # Per-section tokens now account for the whole file (chars//4 rounds per
        # section, so allow one token of slack per section).
        assert abs(sum(s.tokens for s in report.sections) - report.total_tokens) <= len(report.sections)

    def test_extract_refuses_duplicate_headings(self, tmp_path):
        """A merged CLAUDE.md repeats headings; guessing would delete the wrong one."""
        import pytest as _pytest

        from scripts.claude_linter import extract_to_skill

        md = tmp_path / "CLAUDE.md"
        md.write_text("## Security\nfrom bundle\n\n## Other\nx\n\n## Security\nfrom profile\n")
        with _pytest.raises(ValueError, match="appears 2 times"):
            extract_to_skill(md, "Security", "sec", output_dir=tmp_path / "out")

    def test_classify_always_section(self):
        from scripts.claude_linter import Section, classify_section

        s = Section(
            heading="Architecture", level=2, content="## Architecture\nDesign patterns.", start_line=1, end_line=3
        )
        assert classify_section(s) == "always"

    def test_classify_workflow_section(self):
        from scripts.claude_linter import Section, classify_section

        # Must be >400 chars (>100 tokens) with multiple workflow signals
        content = (
            "## Deploy\n"
            "When the user asks to deploy, use this workflow.\n"
            "If the user wants to rollback, trigger the /rollback command.\n"
            "Step 1: Build the project artifacts.\n"
            "Step 2: Push to the remote server and verify health checks pass.\n"
            "This is additional context to make the section long enough. " * 5
        )
        s = Section(heading="Deploy", level=2, content=content, start_line=1, end_line=10)
        assert classify_section(s) == "workflow"

    def test_short_section_is_always(self):
        from scripts.claude_linter import Section, classify_section

        s = Section(heading="Short", level=2, content="## Short\nSmall.", start_line=1, end_line=2)
        assert classify_section(s) == "always"

    def test_lint_report(self, tmp_path):
        from scripts.claude_linter import lint_report

        padding = "This is a long section with lots of text to pad the content. " * 8
        md = tmp_path / "CLAUDE.md"
        md.write_text(
            "# Project\n\n"
            "## Commands\nRun tests.\n\n"
            "## Deploy Workflow\n"
            "When the user asks to deploy, use this workflow.\n"
            "Step 1: Do this. Step 2: Do that.\n"
            "If the user wants rollback, use the rollback command.\n"
            f"{padding}\n"
        )

        report = lint_report(md)
        assert report.total_tokens > 0
        # H1 title preamble + the two H2 sections
        assert len(report.sections) == 3

    def test_format_report(self, tmp_path):
        from scripts.claude_linter import format_report, lint_report

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Project\n\n## Commands\nRun tests.\n\n## Architecture\nDesign.\n")

        report = lint_report(md)
        output = format_report(report)
        assert "CLAUDE.md Lint Report" in output
        assert "Commands" in output

    def test_extract_to_skill(self, tmp_path):
        from scripts.claude_linter import extract_to_skill

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Project\n\n## Commands\nRun tests.\n\n## Architecture\nDesign.\n")
        output_dir = tmp_path / "skills"

        result = extract_to_skill(md, "Commands", "cmds", output_dir)
        assert result.exists()
        assert "Run tests" in result.read_text()

        # Section should be removed from CLAUDE.md
        remaining = md.read_text()
        assert "## Commands" not in remaining
        assert "## Architecture" in remaining

    def test_extract_nonexistent_raises(self, tmp_path):
        from scripts.claude_linter import extract_to_skill

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Project\n\n## Commands\nRun tests.\n")

        with pytest.raises(ValueError, match="not found"):
            extract_to_skill(md, "Nonexistent", "test", tmp_path / "out")
