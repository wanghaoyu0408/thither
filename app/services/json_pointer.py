"""Minimal RFC 6901 JSON Pointer support.

Written here rather than pulled from a library so that a failed patch can name
the exact operation, pointer and reason. The agent needs that detail to correct
itself on the next turn; a generic KeyError would not help it.
"""

from typing import Any


class PointerError(ValueError):
    """A pointer is malformed, or does not resolve against the document."""


def escape(token: str) -> str:
    """Encode a literal string for use as one pointer token."""
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    # ~1 must be decoded before ~0, per RFC 6901 section 4.
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PointerError(f"pointer must be empty or start with '/': {pointer!r}")
    return [_unescape(token) for token in pointer.split("/")[1:]]


def build_pointer(*tokens: str | int) -> str:
    return "".join(f"/{escape(str(token))}" for token in tokens)


def _array_index(token: str, length: int, *, allow_append: bool, pointer: str) -> int:
    if token == "-":
        if not allow_append:
            raise PointerError(f"'-' is only meaningful for add operations: {pointer!r}")
        return length
    if not (token.isascii() and token.isdigit()) or (len(token) > 1 and token[0] == "0"):
        raise PointerError(f"invalid array index {token!r} in {pointer!r}")
    index = int(token)
    limit = length if allow_append else length - 1
    if index > limit:
        raise PointerError(f"array index {index} out of range (length {length}) in {pointer!r}")
    return index


def _descend(container: Any, token: str, pointer: str, depth: int) -> Any:
    if isinstance(container, dict):
        if token not in container:
            raise PointerError(f"key {token!r} not found at depth {depth} in {pointer!r}")
        return container[token]
    if isinstance(container, list):
        return container[_array_index(token, len(container), allow_append=False, pointer=pointer)]
    raise PointerError(
        f"cannot descend into {type(container).__name__} at token {token!r} in {pointer!r}"
    )


def resolve(doc: Any, pointer: str) -> Any:
    current = doc
    for depth, token in enumerate(parse_pointer(pointer)):
        current = _descend(current, token, pointer, depth)
    return current


def _resolve_parent(doc: Any, pointer: str) -> tuple[Any, str]:
    tokens = parse_pointer(pointer)
    if not tokens:
        raise PointerError("operations on the document root are not allowed")
    parent = doc
    for depth, token in enumerate(tokens[:-1]):
        parent = _descend(parent, token, pointer, depth)
    return parent, tokens[-1]


def set_value(doc: Any, pointer: str, value: Any) -> None:
    """Replace the value at an existing location, or create an object key."""
    parent, token = _resolve_parent(doc, pointer)
    if isinstance(parent, dict):
        parent[token] = value
    elif isinstance(parent, list):
        parent[_array_index(token, len(parent), allow_append=False, pointer=pointer)] = value
    else:
        raise PointerError(f"cannot set on {type(parent).__name__} in {pointer!r}")


def add_value(doc: Any, pointer: str, value: Any) -> None:
    """Insert into an array (index, or '-' to append), or create an object key."""
    parent, token = _resolve_parent(doc, pointer)
    if isinstance(parent, dict):
        parent[token] = value
    elif isinstance(parent, list):
        index = _array_index(token, len(parent), allow_append=True, pointer=pointer)
        parent.insert(index, value)
    else:
        raise PointerError(f"cannot add to {type(parent).__name__} in {pointer!r}")


def remove_value(doc: Any, pointer: str) -> Any:
    """Delete an array element or object key, returning what was removed."""
    parent, token = _resolve_parent(doc, pointer)
    if isinstance(parent, dict):
        if token not in parent:
            raise PointerError(f"key {token!r} not found, cannot remove {pointer!r}")
        return parent.pop(token)
    if isinstance(parent, list):
        return parent.pop(_array_index(token, len(parent), allow_append=False, pointer=pointer))
    raise PointerError(f"cannot remove from {type(parent).__name__} in {pointer!r}")
