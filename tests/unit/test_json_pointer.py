import pytest

from app.services import json_pointer as jp


def test_parse_root_and_tokens():
    assert jp.parse_pointer("") == []
    assert jp.parse_pointer("/a/b/c") == ["a", "b", "c"]
    assert jp.parse_pointer("/") == [""]


def test_parse_requires_leading_slash():
    with pytest.raises(jp.PointerError):
        jp.parse_pointer("a/b")


def test_escaping_decodes_slash_before_tilde():
    # RFC 6901 section 4: ~1 then ~0, so "~01" must decode to "~1", not "/".
    assert jp.parse_pointer("/a~1b") == ["a/b"]
    assert jp.parse_pointer("/a~0b") == ["a~b"]
    assert jp.parse_pointer("/~01") == ["~1"]
    assert jp.escape("a/b~c") == "a~1b~0c"


def test_build_pointer_escapes():
    assert jp.build_pointer("entities", "a/b") == "/entities/a~1b"
    assert jp.build_pointer("itinerary", "days", 2) == "/itinerary/days/2"


def test_resolve_nested():
    doc = {"a": {"b": [10, 20, {"c": "found"}]}}
    assert jp.resolve(doc, "/a/b/0") == 10
    assert jp.resolve(doc, "/a/b/2/c") == "found"
    assert jp.resolve(doc, "") == doc


def test_resolve_missing_key_reports_pointer():
    with pytest.raises(jp.PointerError, match="not found"):
        jp.resolve({"a": 1}, "/nope")


def test_resolve_cannot_descend_into_scalar():
    with pytest.raises(jp.PointerError, match="cannot descend"):
        jp.resolve({"a": 1}, "/a/b")


def test_set_replaces_and_creates_object_keys():
    doc = {"a": {"b": 1}}
    jp.set_value(doc, "/a/b", 2)
    assert doc["a"]["b"] == 2

    jp.set_value(doc, "/a/new", "x")
    assert doc["a"]["new"] == "x"


def test_set_on_list_index():
    doc = {"items": [1, 2, 3]}
    jp.set_value(doc, "/items/1", 99)
    assert doc["items"] == [1, 99, 3]


def test_set_out_of_range():
    with pytest.raises(jp.PointerError, match="out of range"):
        jp.set_value({"items": [1]}, "/items/5", 0)


def test_set_rejects_dash():
    with pytest.raises(jp.PointerError, match="only meaningful for add"):
        jp.set_value({"items": [1]}, "/items/-", 0)


def test_add_appends_with_dash():
    doc = {"items": [1, 2]}
    jp.add_value(doc, "/items/-", 3)
    assert doc["items"] == [1, 2, 3]


def test_add_inserts_at_index_including_end():
    doc = {"items": [1, 3]}
    jp.add_value(doc, "/items/1", 2)
    assert doc["items"] == [1, 2, 3]

    jp.add_value(doc, "/items/3", 4)
    assert doc["items"] == [1, 2, 3, 4]


def test_add_beyond_end_rejected():
    with pytest.raises(jp.PointerError, match="out of range"):
        jp.add_value({"items": [1]}, "/items/5", 0)


def test_remove_key_and_index():
    doc = {"a": 1, "items": [1, 2, 3]}
    assert jp.remove_value(doc, "/a") == 1
    assert "a" not in doc

    assert jp.remove_value(doc, "/items/1") == 2
    assert doc["items"] == [1, 3]


def test_remove_missing_key():
    with pytest.raises(jp.PointerError, match="cannot remove"):
        jp.remove_value({"a": 1}, "/b")


def test_remove_from_empty_list():
    with pytest.raises(jp.PointerError, match="out of range"):
        jp.remove_value({"items": []}, "/items/0")


@pytest.mark.parametrize("token", ["01", "1x", "-1", "+1", "١"])
def test_invalid_array_indices(token):
    with pytest.raises(jp.PointerError, match="invalid array index"):
        jp.resolve({"items": [1, 2]}, f"/items/{token}")


def test_root_mutation_rejected():
    for mutate in (
        lambda: jp.set_value({}, "", 1),
        lambda: jp.add_value({}, "", 1),
        lambda: jp.remove_value({}, ""),
    ):
        with pytest.raises(jp.PointerError, match="root"):
            mutate()
