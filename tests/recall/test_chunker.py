import pytest

from scripts.recall.chunker import chunk_body


def test_short_body_stays_whole():
    assert chunk_body("# One\n\nShort\n\n# Two\n\nBody") == ["# One\n\nShort\n\n# Two\n\nBody"]
    assert chunk_body("  body\n") == ["body"]
    assert chunk_body(" ") == []
    assert chunk_body("") == []


def test_long_body_splits_at_headings_then_paragraphs():
    body = "# One\n\n" + "a" * 700 + "\n\n" + "b" * 700 + "\n\n## Two\n\n" + "c" * 600
    assert chunk_body(body) == ["# One\n\n" + "a" * 700, "b" * 700, "## Two\n\n" + "c" * 600]


def test_oversized_paragraph_splits_without_losing_characters():
    body = "abcdefghij" * 300
    chunks = chunk_body(body)
    assert list(map(len, chunks)) == [1200, 1200, 600]
    assert "".join(chunks) == body
    assert chunks == chunk_body(body)


def test_paragraph_windows_accept_exact_limit_and_keep_final_window():
    body = "a" * 600 + "\n\n" + "b" * 598 + "\n\n" + "c"
    assert chunk_body(body) == ["a" * 600 + "\n\n" + "b" * 598, "c"]


def test_heading_levels_and_nonheading_hashes():
    body = "prefix\n\n" + "a" * 1200 + "\n\n###### Six\n\nText # inside\n####### Not a heading"
    chunks = chunk_body(body)
    assert chunks == ["prefix", "a" * 1200, "###### Six\n\nText # inside\n####### Not a heading"]


def test_custom_window_size():
    assert chunk_body("abcd\n\nefgh\n\nij", window=10) == ["abcd\n\nefgh", "ij"]
    assert chunk_body("abcdefghijk", window=4) == ["abcd", "efgh", "ijk"]


def test_one_character_window_and_exact_size_body():
    assert chunk_body("ab", window=1) == ["a", "b"]
    body = "# One\n\n# Two"
    assert chunk_body(body, window=len(body)) == [body]


@pytest.mark.parametrize("marker", ["```", "~~~~"])
def test_fenced_heading_is_not_a_section_boundary(marker):
    fenced = f"{marker}python\n# comment\n" + "b" * 100 + f"\n{marker}"
    body = "a" * 1100 + "\n\n" + fenced
    assert chunk_body(body) == ["a" * 1100, fenced]


@pytest.mark.parametrize("opening,invalid", [("````", "~~~"), ("````", "```"), ("```", "```suffix")])
def test_only_matching_fence_closes_and_following_heading_splits(opening, invalid):
    fenced = f"{opening}python\n{invalid}\n# Inside\n" + "b" * 100 + f"\n{opening}"
    body = "a" * 1100 + "\n\n" + fenced + "\n\n## After\nTail"
    assert chunk_body(body) == ["a" * 1100, fenced, "## After\nTail"]


@pytest.mark.parametrize("window", [0, -1])
def test_window_must_be_positive(window):
    with pytest.raises(ValueError, match="^window must be positive$"):
        chunk_body("body", window=window)
