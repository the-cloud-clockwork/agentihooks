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


@pytest.mark.parametrize("window", [0, -1])
def test_window_must_be_positive(window):
    with pytest.raises(ValueError, match="window must be positive"):
        chunk_body("body", window=window)
