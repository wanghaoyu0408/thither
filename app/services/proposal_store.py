"""Short-lived storage for proposed itinerary changes.

`generate_itinerary` and `replan_day` hand back a proposal; the model reviews
the summary and validation report, then calls `apply_trip_patch` with just the
`proposal_id`. That keeps a whole itinerary out of the model's output tokens -
cheaper, and one fewer place for it to mangle the state on the way through.

In-process and deliberately short-lived: a proposal is only meaningful against
the revision it was built from, so surviving a restart would be a liability.
"""

from datetime import timedelta

from app.models.common import utcnow
from app.models.itinerary_plan import ItineraryProposal

DEFAULT_TTL = timedelta(minutes=30)


class ProposalStore:
    def __init__(self, ttl: timedelta = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._entries: dict[str, ItineraryProposal] = {}
        self._created: dict[str, object] = {}

    def put(self, proposal: ItineraryProposal) -> str:
        self._purge()
        self._entries[proposal.proposal_id] = proposal
        self._created[proposal.proposal_id] = utcnow()
        return proposal.proposal_id

    def get(self, proposal_id: str) -> ItineraryProposal | None:
        self._purge()
        return self._entries.get(proposal_id)

    def discard(self, proposal_id: str) -> None:
        self._entries.pop(proposal_id, None)
        self._created.pop(proposal_id, None)

    def _purge(self) -> None:
        cutoff = utcnow() - self._ttl
        for proposal_id, created in list(self._created.items()):
            if created < cutoff:
                self.discard(proposal_id)

    def __len__(self) -> int:
        self._purge()
        return len(self._entries)
