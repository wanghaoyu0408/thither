"""Shared traversal of a serialized TripState.

Lock enforcement and rejection memory both need to visit every decision. They
used to walk `state["decisions"].values()` independently, which quietly assumed
every value *is* a decision. Adding `place_shortlists` - a dict of decisions -
broke that assumption in two places at once: shortlisted decisions became
invisible to locks, and shortlisted options became invisible to rejection
memory, which is exactly the case the feature exists for.

One walker, imported by both, so the next nested group cannot reopen the gap.
"""

from collections.abc import Iterator
from typing import Any


def iter_decision_dicts(state: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every decision in a serialized TripState, singleton or nested."""
    for value in (state.get("decisions") or {}).values():
        if not isinstance(value, dict):
            continue

        if value.get("decision_id"):
            yield value
            continue

        # A keyed group of decisions, e.g. place_shortlists.
        for nested in value.values():
            if isinstance(nested, dict) and nested.get("decision_id"):
                yield nested
