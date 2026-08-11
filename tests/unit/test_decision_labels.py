"""Two options a person cannot tell apart are not a choice.

Every airport fixture in this suite set `city=iata`, which made the failure
below structurally impossible to reproduce: two airports of one city. That is
why it shipped, and why these use the real municipality.
"""

from app.agent.context import _selected_label
from app.models.decision import Decision, DecisionOption
from app.models.flight import AirportOption
from app.services.decision_service import label_for
from app.services.option_metrics import metrics_for, unscored_for


def chicago(iata: str, name: str, *, minutes: float, km: float) -> AirportOption:
    return AirportOption(
        iata=iata,
        name=name,
        city="Chicago",          # both of them, which is the whole point
        country="US",
        lat=41.9,
        lng=-87.8,
        distance_km=km,
        ground_travel_minutes=minutes,
        ground_travel_source="routes_api",
    )


ORD = chicago("ORD", "Chicago O'Hare International Airport", minutes=24.1, km=24.9)
MDW = chicago("MDW", "Chicago Midway International Airport", minutes=24.2, km=14.7)


# --- the label ---------------------------------------------------------------


def test_two_airports_in_one_city_are_told_apart():
    """Both cards used to read "Chicago". `label_for` returned at `city`, two
    attributes before the `iata` branch that would have separated them."""
    assert label_for(ORD) != label_for(MDW)
    assert label_for(ORD) == "Chicago O'Hare International Airport (ORD)"
    assert label_for(MDW) == "Chicago Midway International Airport (MDW)"


def test_the_model_is_told_which_airport_was_chosen():
    """`_selected_label` reads the same function, so the model could not tell
    them apart either - it would write "Chicago" twice in its own reply."""
    decision = Decision[AirportOption](
        decision_id="dec_arr",
        status="selected",
        options=[
            DecisionOption[AirportOption](option_id="opt_ord", data=ORD, status="selected"),
            DecisionOption[AirportOption](option_id="opt_mdw", data=MDW, status="shortlisted"),
        ],
        selected_option_id="opt_ord",
    )

    assert _selected_label(decision) == "Chicago O'Hare International Airport (ORD)"


def test_other_payloads_keep_the_names_they_had():
    """Only AirportOption carries `iata`, so nothing else changes."""
    from app.models.decision import HotelAreaOption

    assert label_for(HotelAreaOption(area_name="Ueno")) == "Ueno"


# --- the figures -------------------------------------------------------------


def test_an_airport_carries_its_drive_time_and_distance():
    """The branch did not exist, so airport cards showed no numbers at all."""
    figures = {m.label: m.value for m in metrics_for(ORD)}

    assert figures["Drive from the pickup point"] == "24.1 min"
    assert figures["Distance"] == "24.9 km"
    assert "straight line" in next(
        m.note for m in metrics_for(ORD) if m.label == "Distance"
    )


def test_the_decimal_is_what_separates_two_close_airports():
    """Rounded to the minute, 24.1 and 24.2 both read "24" - which is exactly
    the pair a traveller is choosing between."""
    ord_drive = next(m.value for m in metrics_for(ORD) if m.label.startswith("Drive"))
    mdw_drive = next(m.value for m in metrics_for(MDW) if m.label.startswith("Drive"))
    assert ord_drive != mdw_drive

    # And the distance separates them far more sharply than the drive does.
    ord_km = next(m.value for m in metrics_for(ORD) if m.label == "Distance")
    mdw_km = next(m.value for m in metrics_for(MDW) if m.label == "Distance")
    assert ord_km == "24.9 km" and mdw_km == "14.7 km"


def test_an_unmeasured_drive_says_so_rather_than_comparing():
    guessed = ORD.model_copy(update={"ground_travel_source": "unavailable"})
    note = next(m.note for m in metrics_for(guessed) if m.label.startswith("Drive"))
    assert "cannot be compared" in note


def test_an_airport_names_what_nobody_measured():
    unscored = " ".join(unscored_for(ORD))
    assert "Fares" in unscored
    assert "security" in unscored or "terminal" in unscored
