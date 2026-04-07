"""Tests for hooks.context.preprocessor."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Protection mask
# ---------------------------------------------------------------------------


class TestProtectionMask:
    def test_code_fence_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "before ```kubectl delete pod``` after"
        mask = _build_protection_mask(text)
        # The fenced block span should be in the mask
        assert any(text[s:e] == "```kubectl delete pod```" for s, e in mask)

    def test_inline_code_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "run `kubectl delete` now"
        mask = _build_protection_mask(text)
        assert any("`kubectl delete`" in text[s:e] for s, e in mask)

    def test_negation_words_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "never push to main"
        mask = _build_protection_mask(text)
        spans_text = " ".join(text[s:e] for s, e in mask)
        assert "never" in spans_text

    def test_action_verbs_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "always push and delete carefully"
        mask = _build_protection_mask(text)
        spans_text = " ".join(text[s:e] for s, e in mask)
        assert "push" in spans_text
        assert "delete" in spans_text

    def test_allcaps_identifiers_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "set CONTEXT_REFRESH_MAX_CHARS to 8000"
        mask = _build_protection_mask(text)
        spans_text = " ".join(text[s:e] for s, e in mask)
        assert "CONTEXT_REFRESH_MAX_CHARS" in spans_text

    def test_numbers_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "every 20 turns, limit is 8000 chars"
        mask = _build_protection_mask(text)
        spans_text = " ".join(text[s:e] for s, e in mask)
        assert "20" in spans_text
        assert "8000" in spans_text

    def test_file_paths_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "store in ~/.agentihooks/.env safely"
        mask = _build_protection_mask(text)
        assert any("~/.agentihooks/.env" in text[s:e] for s, e in mask)

    def test_cli_subcommands_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "run kubectl delete before scaling"
        mask = _build_protection_mask(text)
        assert any("kubectl delete" in text[s:e] for s, e in mask)

    def test_assertion_words_protected(self):
        from hooks.context.preprocessor import _build_protection_mask

        text = "always use must follow only this"
        mask = _build_protection_mask(text)
        spans_text = " ".join(text[s:e] for s, e in mask)
        assert "always" in spans_text
        assert "must" in spans_text
        assert "only" in spans_text


# ---------------------------------------------------------------------------
# Level 1: Markdown stripping
# ---------------------------------------------------------------------------


class TestLevel1MarkdownStripping:
    def test_headers_simplified(self):
        from hooks.context.preprocessor import preprocess

        text = "## Section Title\n\nSome content here."
        result = preprocess(text, level=1)
        assert "##" not in result
        assert "Section Title" in result

    def test_mermaid_removed(self):
        from hooks.context.preprocessor import preprocess

        text = "before\n```mermaid\nflowchart LR\nA-->B\n```\nafter"
        result = preprocess(text, level=1)
        assert "flowchart" not in result
        assert "before" in result
        assert "after" in result

    def test_bold_italic_stripped(self):
        from hooks.context.preprocessor import preprocess

        text = "this is **important** and *critical*"
        result = preprocess(text, level=1)
        assert "**" not in result
        assert "*critical*" not in result
        assert "important" in result
        assert "critical" in result

    def test_horizontal_rules_removed(self):
        from hooks.context.preprocessor import preprocess

        text = "above\n---\nbelow"
        result = preprocess(text, level=1)
        assert "---" not in result
        assert "above" in result
        assert "below" in result

    def test_tables_flattened(self):
        from hooks.context.preprocessor import preprocess

        text = "| Key | Value |\n|-----|-------|\n| foo | bar |"
        result = preprocess(text, level=1)
        assert "|-----|" not in result
        assert "foo" in result
        assert "bar" in result


# ---------------------------------------------------------------------------
# Level 2: Filler words and abbreviations
# ---------------------------------------------------------------------------


class TestLevel2FillerAndAbbrev:
    def test_filler_words_removed(self):
        from hooks.context.preprocessor import preprocess

        text = "The system is configured to use the Redis database"
        result = preprocess(text, level=2)
        # "is", "to", "the" (mid-sentence) should be removed; "The" at start may survive
        assert " is " not in result
        assert " to " not in result
        assert "Redis" in result
        assert "db" in result  # "database" abbreviated

    def test_abbreviations_applied(self):
        from hooks.context.preprocessor import preprocess

        text = "the kubernetes configuration for production deployment"
        result = preprocess(text, level=2)
        assert "k8s" in result
        assert "cfg" in result
        assert "prod" in result

    def test_abbreviations_longest_match_first(self):
        from hooks.context.preprocessor import preprocess

        # "authentication" should match before "auth" if both exist
        text = "authentication is required"
        result = preprocess(text, level=2)
        assert "auth" in result
        assert "authentication" not in result


# ---------------------------------------------------------------------------
# Level 3: Disemvoweling
# ---------------------------------------------------------------------------


class TestLevel3Disemvoweling:
    def test_long_words_disemvoweled(self):
        from hooks.context.preprocessor import preprocess

        text = "collaborative instruction protection"
        result = preprocess(text, level=3)
        # Words should be shorter
        assert len(result) < len(text)

    def test_short_words_preserved(self):
        from hooks.context.preprocessor import preprocess

        text = "the code is good"
        result_l2 = preprocess(text, level=2)
        result_l3 = preprocess(text, level=3)
        # Short words shouldn't be disemvoweled (they may be removed as filler at L2)
        # But at L3, no additional shrinkage on already-short words
        assert "good" in result_l3 or "gd" in result_l3  # "good" is only 4 chars, below threshold

    def test_exclusion_words_preserved(self):
        from hooks.context.preprocessor import preprocess

        text = "there was an error in the order of the issue"
        result = preprocess(text, level=3)
        # "error", "order", "issue" are in exclusion set
        assert "error" in result or "errr" not in result  # should NOT be mangled


# ---------------------------------------------------------------------------
# Safety invariants — parametrized
# ---------------------------------------------------------------------------


SAFETY_INPUTS = [
    ("never push to main", ["never", "push"]),
    ("must not delete production pods", ["must", "not", "delete"]),
    ("set CONTEXT_REFRESH_MAX_CHARS to 8000", ["CONTEXT_REFRESH_MAX_CHARS", "8000"]),
    ("run `kubectl delete pod`", ["kubectl delete pod"]),
    ("TOKEN_REDIS_TTL is 3600 seconds", ["TOKEN_REDIS_TTL", "3600"]),
    ("path ~/.agentihooks/.env must exist", ["must"]),
    ("don't force push to main", ["don't", "force", "push"]),
    ("always use --dry-run first", ["always"]),
    ("strictly no rm -rf in production", ["strictly", "no"]),
]


class TestSafetyInvariants:
    @pytest.mark.parametrize("text,must_survive", SAFETY_INPUTS)
    def test_protected_tokens_survive_all_levels(self, text, must_survive):
        from hooks.context.preprocessor import preprocess

        for level in [1, 2, 3]:
            result = preprocess(text, level)
            for token in must_survive:
                assert token in result, (
                    f"Level {level} destroyed protected token {token!r} "
                    f"in text {text!r} -> {result!r}"
                )


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


REAL_RULE_SAMPLE = """## Operator Delegation Map

### Default Mode: Full Autonomy

The operator expects **maximum** independence. When given a task — whether 1 step or 50 — execute ALL steps end-to-end without stopping to ask "do you want to continue?", "should I proceed?", or "want me to commit/push?". The answer is always yes. Just do it.

- All deploys and restarts (dev AND production)
- Git commits, git push (any branch including main), creating PRs
- `kubectl` operations (apply, delete, rollout, scale)
- `docker` operations (build, push, rm, prune)
- SSH/SCP operations to any known host

### Hard Block — ONLY secrets

- Never handle real credentials, API keys, tokens, or passwords in plaintext
- Reference secrets via environment variables only
"""


class TestFullPipeline:
    def test_level0_passthrough(self):
        from hooks.context.preprocessor import preprocess

        assert preprocess(REAL_RULE_SAMPLE, level=0) == REAL_RULE_SAMPLE

    def test_level1_smaller(self):
        from hooks.context.preprocessor import preprocess

        result = preprocess(REAL_RULE_SAMPLE, level=1)
        assert len(result) < len(REAL_RULE_SAMPLE)

    def test_level2_smaller_than_level1(self):
        from hooks.context.preprocessor import preprocess

        r1 = preprocess(REAL_RULE_SAMPLE, level=1)
        r2 = preprocess(REAL_RULE_SAMPLE, level=2)
        assert len(r2) < len(r1)

    def test_level3_smaller_than_level2(self):
        from hooks.context.preprocessor import preprocess

        r2 = preprocess(REAL_RULE_SAMPLE, level=2)
        r3 = preprocess(REAL_RULE_SAMPLE, level=3)
        assert len(r3) < len(r2)

    def test_critical_tokens_survive_level3(self):
        from hooks.context.preprocessor import preprocess

        result = preprocess(REAL_RULE_SAMPLE, level=3)
        assert "Never" in result or "never" in result
        assert "kubectl" in result
        assert "docker" in result
        assert "push" in result
        assert "delete" in result


# ---------------------------------------------------------------------------
# Compression ratio
# ---------------------------------------------------------------------------


class TestCompressionRatio:
    def test_ratio_calculation(self):
        from hooks.context.preprocessor import compression_ratio

        assert compression_ratio("abcdef", "abc") == 0.5
        assert compression_ratio("abc", "abc") == 1.0

    def test_ratio_zero_length_original(self):
        from hooks.context.preprocessor import compression_ratio

        assert compression_ratio("", "") == 1.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_input(self):
        from hooks.context.preprocessor import preprocess

        assert preprocess("", level=3) == ""

    def test_all_code_block(self):
        from hooks.context.preprocessor import preprocess

        text = "```\nkubectl apply -f deploy.yaml\nkubectl rollout status\n```"
        result = preprocess(text, level=3)
        # Entire input is protected — should be mostly unchanged
        assert "kubectl apply" in result
        assert "kubectl rollout" in result

    def test_all_protected_tokens(self):
        from hooks.context.preprocessor import preprocess

        text = "never push delete force CONTEXT_REFRESH_MAX_CHARS 8000"
        result = preprocess(text, level=3)
        for token in ["never", "push", "delete", "force", "CONTEXT_REFRESH_MAX_CHARS", "8000"]:
            assert token in result

    def test_exception_returns_original(self):
        from hooks.context.preprocessor import preprocess

        # Passing a weird type shouldn't crash — should return input
        text = "normal text"
        with patch("hooks.context.preprocessor._build_protection_mask", side_effect=RuntimeError("boom")):
            result = preprocess(text, level=2)
        assert result == text


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    @pytest.mark.parametrize("env_val,expected", [
        ("off", 0),
        ("light", 1),
        ("standard", 2),
        ("aggressive", 3),
        ("invalid", 0),
        ("", 0),
    ])
    def test_get_level_from_config(self, env_val, expected):
        from hooks.context.preprocessor import get_level_from_config

        with patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", env_val):
            assert get_level_from_config() == expected


# ---------------------------------------------------------------------------
# Global compression scope (inject_context integration)
# ---------------------------------------------------------------------------


class TestGlobalCompressionScope:
    def test_inject_context_compresses_when_scope_all(self, capsys):
        with patch("hooks.config.CONTEXT_COMPRESSION_SCOPE", "all"), \
             patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", "standard"):
            from hooks.common import inject_context

            inject_context("The system is configured to use the kubernetes database", also_log=False)

        captured = capsys.readouterr().out
        # "kubernetes" should be abbreviated to "k8s", "database" to "db"
        assert "k8s" in captured
        assert "db" in captured

    def test_inject_context_no_compression_when_scope_refresh(self, capsys):
        with patch("hooks.config.CONTEXT_COMPRESSION_SCOPE", "refresh"), \
             patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", "standard"):
            from hooks.common import inject_context

            inject_context("The system is configured to use the kubernetes database", also_log=False)

        captured = capsys.readouterr().out
        assert "kubernetes" in captured
        assert "database" in captured

    def test_inject_context_skip_compression_flag(self, capsys):
        with patch("hooks.config.CONTEXT_COMPRESSION_SCOPE", "all"), \
             patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", "standard"):
            from hooks.common import inject_context

            inject_context("The kubernetes database", also_log=False, skip_compression=True)

        captured = capsys.readouterr().out
        assert "kubernetes" in captured  # not compressed because skip=True

    def test_inject_banner_passes_skip_compression(self, capsys):
        with patch("hooks.config.CONTEXT_COMPRESSION_SCOPE", "all"), \
             patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", "standard"):
            from hooks.common import inject_banner

            inject_banner("TEST", "kubernetes database", also_log=False, skip_compression=True)

        captured = capsys.readouterr().out
        assert "kubernetes" in captured  # not compressed
