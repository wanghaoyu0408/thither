"""A fork the agent reached, put to the traveller as something they can press.

The agent used to describe these in prose and stop. Searching ALB to MDW found
no airline flying it, and the reply said - correctly - that the practical next
step was to reconsider the arrival airport, with ORD the other one already
shortlisted. Then nothing: no card, no button, no way to take the step it had
just named. Every other surface refused the question. `ask_clarifications` will
only ask about an outstanding intake requirement; `OpenQuestion` has no choices
and nothing anywhere can answer one; the decision chooser hides a decision that
is already settled, which the arrival airport was.

**The actions are a closed set.** A proposal cannot carry an arbitrary patch -
each choice names something the system already knows how to do, the same
discipline as `record_constraints`' category enum and `ask_clarifications`
having to cite a requirement_id. A model with a proposal in mind will find a
way to phrase it; what it may actually *do* is fixed here.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import new_id, utcnow

# What pressing a choice does.
#   select_option - settle a decision on one of its existing options
#   set_aside     - stop pursuing a part for now, saying why
#   resume        - pick a set-aside part back up
#   none          - acknowledge and carry on; nothing changes
ProposalAction = Literal["select_option", "set_aside", "resume", "none"]


class ProposalChoice(BaseModel):
    """One answer, and what it does.

    `note` is where a choice says what it costs - "a longer drive from the
    airport", "flights stay unplanned until you say otherwise" - so the
    traveller is not choosing between two labels alone.
    """

    label: str
    action: ProposalAction = "none"

    # For select_option: which decision, and which of its options.
    decision: str | None = None
    option_id: str | None = None

    # For set_aside / resume: "flights" or "lodging".
    part: str | None = None

    note: str | None = None


class AgentProposal(BaseModel):
    """A question with actions attached, asked once and answered once.

    Stored rather than derived, unlike a learning hypothesis: this one is a
    fact about a fork that was actually reached, and there is nothing to
    recompute it from once the turn is over.
    """

    proposal_id: str = Field(default_factory=lambda: new_id("prp"))

    question: str
    # Why it is being asked, from what the tools actually returned. Never a
    # rationalisation composed afterwards.
    detail: str | None = None

    choices: list[ProposalChoice] = Field(min_length=2)

    asked_at: datetime = Field(default_factory=utcnow)
    answered_at: datetime | None = None
    chosen_label: str | None = None

    @property
    def answered(self) -> bool:
        return self.answered_at is not None
