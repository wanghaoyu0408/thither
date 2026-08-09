"""Explanations come from what was stored, or they say they cannot.

The failure this guards against is the fluent one: a model asked "why did you
pick this?" always answers, and sometimes the answer is invented. So the
explanation is assembled from persisted decision data, and when that data is not
there the result says so.
"""

from datetime import datetime

from app.models.decision import Decision, DecisionOption, DecisionScore, PlaceOption
from app.models.evidence import EvidenceRecord
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.services.explanation_service import explain, explain_item
from app.services.group_scoring import build_group_score
from tests.conftest import make_entity, sample_state


def evidence(evidence_id: str, *, authority, source_type, entity_id="ent_cafe") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        url=f"https://example.com/{evidence_id}",
        title=f"Source {evidence_id}",
        source_type=source_type,
        source_authority=authority,
        summary="Mentions the place favourably.",
        entity_ids=[entity_id],
        sentiment="positive",
        themes=["seafood"],
    )


def trip_with_recommendation(*, score=True, tradeoff=True, evidence_ids=None, group=None):
    state = sample_state()
    state.decisions.place_shortlists["dinner_asakusa"] = Decision[PlaceOption](
        decision_id="dec_dinner",
        status="shortlisted",
        options=[
            DecisionOption[PlaceOption](
                option_id="opt_cafe",
                data=PlaceOption(entity_id="ent_cafe", purpose="dinner", why="fits the route"),
                status="shortlisted",
                score=DecisionScore(total=0.82, dimensions={"rating": 0.8}, coverage=0.9)
                if score
                else None,
                group_score=group,
                pros=["4.3 from 2,300 reviews"] if tradeoff else [],
                cons=["queues at lunch"] if tradeoff else [],
                evidence_refs=list(evidence_ids or []),
            )
        ],
    )
    return state


def test_an_explanation_is_built_from_the_stored_decision():
    state = trip_with_recommendation()

    result = explain(state, "ent_cafe")

    assert result.complete
    assert result.decision == "place_shortlists.dinner_asakusa"
    assert result.purpose == "dinner"
    assert result.status == "shortlisted"
    assert result.score.total == 0.82
    assert result.pros == ["4.3 from 2,300 reviews"]
    assert result.cons == ["queues at lunch"]
    # Facts about the place itself come from the registry.
    assert result.rating == 4.3
    assert result.rating_count == 2300


def test_a_place_nothing_recommended_reports_incomplete_rather_than_inventing():
    """The common case for anything scheduled before reasoning was recorded."""
    state = sample_state()

    result = explain(state, "ent_cafe")

    assert result.complete is False
    assert any("was not recorded" in gap for gap in result.missing)
    assert result.pros == []
    assert result.cons == []


def test_a_recommendation_with_no_trade_off_is_not_complete():
    state = trip_with_recommendation(tradeoff=False)

    result = explain(state, "ent_cafe")

    assert result.complete is False
    assert any("trade-off" in gap for gap in result.missing)


def test_a_recommendation_with_no_ranking_is_not_complete():
    state = trip_with_recommendation(score=False)

    result = explain(state, "ent_cafe")

    assert result.complete is False
    assert any("ranking" in gap for gap in result.missing)


def test_evidence_is_grouped_by_what_kind_of_claim_it_makes():
    """Google's opening hours and a Reddit opinion are not the same claim."""
    state = trip_with_recommendation(evidence_ids=["ev_o", "ev_e", "ev_c"])
    state.evidence = {
        "ev_o": evidence("ev_o", authority="official", source_type="official"),
        "ev_e": evidence("ev_e", authority="editorial", source_type="publication"),
        "ev_c": evidence("ev_c", authority="community", source_type="reddit"),
    }

    result = explain(state, "ent_cafe")

    assert [e.evidence_id for e in result.official] == ["ev_o"]
    assert [e.evidence_id for e in result.editorial] == ["ev_e"]
    assert [e.evidence_id for e in result.community] == ["ev_c"]
    assert result.evidence_count == 3


def test_unknown_authority_is_treated_as_community_not_as_fact():
    state = trip_with_recommendation(evidence_ids=["ev_x"])
    state.evidence = {"ev_x": evidence("ev_x", authority="unknown", source_type="other")}

    result = explain(state, "ent_cafe")

    assert result.official == []
    assert [e.evidence_id for e in result.community] == ["ev_x"]


def test_a_group_split_survives_into_the_explanation():
    group = build_group_score(
        {"trv_a": 0.9, "trv_b": 0.2}, {"trv_a": "Haoyu", "trv_b": "Alice"}
    )
    state = trip_with_recommendation(group=group)

    result = explain(state, "ent_cafe")

    assert result.group_is_split
    assert result.worst_served == "Alice"
    # Unhappiest first, and never merged into one number.
    assert [t.name for t in result.per_traveler] == ["Alice", "Haoyu"]
    assert result.per_traveler[0].score == 0.2


def test_a_quote_is_trimmed_so_a_citation_cannot_become_a_reproduction():
    state = trip_with_recommendation(evidence_ids=["ev_q"])
    record = evidence("ev_q", authority="community", source_type="reddit")
    record.quote = " ".join(f"word{n}" for n in range(40))
    state.evidence = {"ev_q": record}

    quoted = explain(state, "ent_cafe").community[0].quote

    assert quoted.endswith("...")
    assert len(quoted.split()) <= 16


def test_an_explanation_for_a_scheduled_item_resolves_through_its_place():
    state = trip_with_recommendation()

    result = explain_item(state, "item_dinner")

    assert result is not None
    assert result.entity_id == "ent_cafe"
    assert result.complete


def test_free_time_has_nothing_to_explain_and_says_so():
    state = sample_state()
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=state.brief.dates.start,
                items=[
                    ItineraryItem(
                        item_id="item_free",
                        type="free_time",
                        title="Beach / free time",
                        start_at=datetime(2026, 10, 3, 15, 0),
                    )
                ],
            )
        ]
    )

    result = explain_item(state, "item_free")

    assert result.complete is False
    assert any("not a place" in gap for gap in result.missing)


def test_an_unknown_item_is_none_rather_than_an_empty_explanation():
    assert explain_item(sample_state(), "item_nope") is None


def test_a_place_outside_the_registry_is_named_as_a_gap():
    state = sample_state()
    state.entities.pop("ent_cafe")
    state.itinerary = TripItinerary()

    result = explain(state, "ent_cafe")

    assert result.complete is False
    assert any("registry" in gap for gap in result.missing)


def test_the_explanation_finds_options_in_singleton_decisions_too():
    """Hotels and flights are explained the same way shortlisted places are."""
    state = sample_state()
    state.entities["ent_hotel"] = make_entity("ent_hotel", "Andaz")
    state.decisions.place_shortlists["stay"] = Decision[PlaceOption](
        options=[
            DecisionOption[PlaceOption](
                data=PlaceOption(entity_id="ent_hotel", purpose="hotel"),
                score=DecisionScore(total=0.7),
                pros=["18 min average drive"],
            )
        ]
    )

    assert explain(state, "ent_hotel").complete
