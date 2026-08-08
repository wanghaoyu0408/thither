"""Rejection memory.

When the user says no to a place, the agent must stop putting it back. This
blocks *re-introduction* only: an id already present before the patch stays
allowed, so recording a rejection never retroactively invalidates the state it
was recorded against.
"""

from typing import Any

from app.models.patch import PatchError
from app.models.rejection import RejectionRecord
from app.services.state_walk import iter_decision_dicts

# (context, target_id). The context matters: an entity can already sit in the
# registry and still be newly *scheduled*, which is a fresh recommendation. A
# flat set of ids would miss that.
Reference = tuple[str, str]


def referenced_targets(state: dict[str, Any]) -> set[Reference]:
    """Every place the trip currently commits to something, by context.

    The entity registry counts as a commitment: putting a place back into it is
    the first step of recommending it again, and search results are not
    supposed to be persisted there in the first place.
    """
    refs: set[Reference] = {("entity_registry", eid) for eid in state.get("entities") or {}}

    for day in (state.get("itinerary") or {}).get("days") or []:
        for item in day.get("items") or []:
            if item.get("entity_id"):
                refs.add(("itinerary", item["entity_id"]))

    for decision in iter_decision_dicts(state):
        for option in decision.get("options") or []:
            if option.get("status") in ("shortlisted", "selected") and option.get("option_id"):
                refs.add(("decision_option", option["option_id"]))
        if decision.get("selected_option_id"):
            refs.add(("decision_selection", decision["selected_option_id"]))

    return refs


def check_rejections(
    before: dict[str, Any],
    after: dict[str, Any],
    rejections: list[RejectionRecord],
    allow_rejected: list[str],
) -> list[PatchError]:
    allowed = set(allow_rejected)
    blocked = {r.target_id: r for r in rejections if r.target_id not in allowed}
    if not blocked:
        return []

    new_references = referenced_targets(after) - referenced_targets(before)
    reintroduced = {target_id for _context, target_id in new_references} & blocked.keys()

    return [
        PatchError(
            code="REJECTION_VIOLATION",
            message=(
                f"{blocked[target_id].label or target_id!r} was rejected earlier"
                + (f" ({blocked[target_id].reason})" if blocked[target_id].reason else "")
                + "; name it in allow_rejected to reconsider it"
            ),
            details=[target_id],
        )
        for target_id in sorted(reintroduced)
    ]
