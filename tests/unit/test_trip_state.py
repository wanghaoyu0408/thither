from datetime import date

from app.models import TripState, TripSummary
from tests.conftest import sample_state


def test_new_trip_starts_at_revision_zero():
    state = TripState.new(title="Tokyo", created_by="user_1")

    assert state.revision == 0
    assert state.status == "draft"
    assert state.schema_version == "1.0"
    assert state.trip_id.startswith("trip_")
    assert state.metadata.title == "Tokyo"
    assert state.metadata.created_by == "user_1"


def test_defaults_are_not_shared_between_instances():
    first, second = TripState.new(), TripState.new()
    first.brief.priorities.append("food")

    assert second.brief.priorities == []
    assert first.trip_id != second.trip_id


def test_serialization_round_trip_preserves_everything():
    state = sample_state()

    restored = TripState.model_validate(state.model_dump(mode="json"))

    assert restored == state


def test_round_trip_keeps_generic_decision_payload_typed():
    restored = TripState.model_validate(sample_state().model_dump(mode="json"))

    option = restored.decisions.destination.options[0]
    assert option.data.city == "Tokyo"
    assert option.data.country == "Japan"


def test_summary_projection():
    summary = TripSummary.from_state(sample_state())

    assert summary.destination == "Tokyo"
    assert summary.start_date == date(2026, 10, 3)
    assert summary.traveler_count == 2
    assert summary.revision == 0


def test_party_size_counts_children():
    state = sample_state()
    state.brief.party.children = 1

    assert state.brief.party.size == 5
