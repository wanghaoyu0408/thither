"""Citation handling and source classification.

The rule under test is the one that keeps a fabricated link out of a trip:
**URLs come from citation annotations, never from model prose.**
"""

from types import SimpleNamespace

import pytest

from app.providers.openai_research import _collect, _first_json, classify_source, trim_quote


def annotation(url: str, end: int, title: str = "") -> SimpleNamespace:
    return SimpleNamespace(type="url_citation", url=url, title=title, end_index=end, start_index=0)


def response(text: str, annotations: list) -> SimpleNamespace:
    part = SimpleNamespace(text=text, annotations=annotations)
    return SimpleNamespace(output=[SimpleNamespace(type="message", content=[part])])


# --- the invented-URL trap ---------------------------------------------------


def test_a_url_written_in_prose_is_never_treated_as_a_source():
    """A model asked for sources will happily make one up. Only citations count."""
    prose = "Try Totoya, see https://totally-made-up-site.example/review for details."
    text, citations = _collect(response(prose, [annotation("https://reddit.com/r/x/", 12)]))

    urls = [citation.url for citation in citations]
    assert urls == ["https://reddit.com/r/x/"]
    assert not any("made-up" in url for url in urls)


def test_prose_with_no_annotations_yields_no_citations():
    text, citations = _collect(response("Great ramen at https://example.com/a", []))

    assert citations == []
    assert "example.com" in text  # the prose is kept; it is just not a source


# --- markers -----------------------------------------------------------------


def test_each_source_gets_a_numbered_marker():
    text, citations = _collect(
        response(
            "Totoya is good. Futaku too.",
            [annotation("https://reddit.com/a", 16), annotation("https://blog.example/b", 27)],
        )
    )

    assert [c.index for c in citations] == [1, 2]
    assert "[1]" in text and "[2]" in text
    assert text.index("[1]") < text.index("[2]")


def test_the_same_url_cited_twice_is_one_source():
    text, citations = _collect(
        response(
            "Totoya is good. Also Totoya at night.",
            [annotation("https://reddit.com/a", 15), annotation("https://reddit.com/a", 36)],
        )
    )

    assert len(citations) == 1
    assert text.count("[1]") == 2


def test_markers_do_not_corrupt_earlier_offsets():
    """Inserted from the end backwards, or every later offset shifts."""
    text, _ = _collect(
        response(
            "AAAA BBBB CCCC",
            [annotation("https://a.example", 4), annotation("https://b.example", 9)],
        )
    )

    assert text.startswith("AAAA[1] BBBB[2]")


def test_an_out_of_range_offset_is_clamped_rather_than_crashing():
    text, citations = _collect(response("Short", [annotation("https://a.example", 9_999)]))

    assert citations
    assert text.endswith("[1]")


def test_annotations_of_other_kinds_are_ignored():
    other = SimpleNamespace(type="file_citation", url="https://a.example", end_index=1)
    _text, citations = _collect(response("Hello", [other]))

    assert citations == []


def test_an_annotation_without_a_url_is_skipped():
    broken = SimpleNamespace(type="url_citation", url=None, title="x", end_index=1)
    _text, citations = _collect(response("Hello", [broken]))

    assert citations == []


# --- source classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.xiaohongshu.com/explore/abc", "xiaohongshu"),
        ("https://xhslink.com/abc", "xiaohongshu"),
        ("https://www.reddit.com/r/JapanTravel/comments/x/", "reddit"),
        ("https://old.reddit.com/r/x/", "reddit"),
        ("https://tabelog.com/tokyo/A1301/", "publication"),
        ("https://www.gotokyo.org/en/", "official"),
        ("https://www.japan.travel/en/", "official"),
        ("https://someones-travel-diary.net/tokyo", "blog"),
    ],
)
def test_sources_are_classified_by_host(url, expected):
    assert classify_source(url) == expected


def test_a_lookalike_host_does_not_pass_as_the_real_one():
    """`reddit.com.evil.example` is not Reddit."""
    assert classify_source("https://reddit.com.evil.example/x") == "blog"


def test_an_unparseable_url_is_other():
    assert classify_source("not a url at all") == "other"


# --- quoting -----------------------------------------------------------------


def test_a_long_quote_is_cut_to_a_citation_length():
    long = " ".join(f"word{i}" for i in range(40))

    trimmed = trim_quote(long)

    assert len(trimmed.split()) <= 16
    assert trimmed.endswith("...")


def test_a_short_quote_is_left_alone():
    assert trim_quote("worth the queue") == "worth the queue"


def test_no_quote_stays_none():
    assert trim_quote(None) is None
    assert trim_quote("") is None


# --- extraction payload ------------------------------------------------------


def test_json_is_read_out_of_the_message():
    payload = _first_json(response('{"mentions": [{"name": "Totoya"}]}', []))

    assert payload["mentions"][0]["name"] == "Totoya"


def test_unparseable_output_yields_an_empty_payload():
    assert _first_json(response("not json", [])) == {}
