"""Turning names from the internet into places Google confirms.

Half the value here is the matches that get refused. A wrong match puts a
confident recommendation for the wrong restaurant into someone's trip.
"""

import pytest

from app.models.place import PlaceSummary
from app.models.research import MentionedEntity
from app.services.resolution_service import apply_match, match_mention, normalize, tokens


def place(place_id: str, name: str) -> PlaceSummary:
    return PlaceSummary(place_id=place_id, name=name, lat=35.7, lng=139.8)


# --- normalization -----------------------------------------------------------


def test_case_and_punctuation_are_folded():
    assert normalize("Fuglen Tokyo!") == normalize("fuglen  tokyo")


def test_full_width_characters_fold_to_half_width():
    """The same Japanese venue is routinely written both ways."""
    assert normalize("ＦＵＴＡＫＵ") == normalize("FUTAKU")


def test_noise_words_carry_no_weight():
    assert tokens("The Ramen Restaurant Tokyo") == {"ramen"}


# --- matching ----------------------------------------------------------------


def test_an_exact_name_matches():
    match = match_mention("Sakaba Totoya", [place("a", "Sakaba Totoya")])

    assert match.accepted
    assert match.confidence == "exact"
    assert match.place.place_id == "a"


def test_a_mention_contained_in_the_google_name_matches():
    match = match_mention("Fuglen", [place("a", "Fuglen Tokyo")])

    assert match.accepted
    assert match.confidence == "strong"


def test_a_google_name_contained_in_the_mention_matches():
    match = match_mention("Fuglen Tokyo Tomigaya", [place("a", "Fuglen")])

    assert match.accepted


def test_an_exact_match_beats_a_containment_one():
    match = match_mention(
        "Totoya", [place("long", "Totoya Annex Branch Two"), place("exact", "Totoya")]
    )

    assert match.place.place_id == "exact"


def test_a_name_with_no_overlap_is_refused():
    match = match_mention("Somewhere Entirely Different", [place("a", "Sakaba Totoya")])

    assert match.accepted is False
    assert match.place is None
    assert "unresolved rather than guessed" in match.note


def test_no_candidates_is_refused_rather_than_reaching():
    match = match_mention("Sakaba Totoya", [])

    assert match.accepted is False
    assert "no Google candidates" in match.note


def test_a_mention_of_only_noise_words_is_refused():
    """ "The Restaurant" identifies nothing and must not match the first result."""
    match = match_mention("The Restaurant", [place("a", "Sakaba Totoya")])

    assert match.accepted is False
    assert "no distinguishing words" in match.note


def test_a_word_break_difference_in_the_same_name_matches():
    """Seen live: 'Shimo-Kitazawa' against 'Shimokitazawa' is one place, not two."""
    match = match_mention(
        "Brooklyn Roasting Company Shimokitazawa",
        [place("a", "Brooklyn Roasting Company Shimo-Kitazawa")],
    )

    assert match.accepted
    assert match.confidence == "exact"


def test_squashing_does_not_make_different_places_match():
    match = match_mention("Ogawa Coffee", [place("a", "Ogura Coffee")])

    assert match.accepted is False


def test_a_partial_overlap_is_weak_and_therefore_refused():
    match = match_mention("Tokyo Ramen Ichiban Kagurazaka", [place("a", "Ichiban Sushi Ginza")])

    assert match.accepted is False


def test_matching_is_stable_across_equally_good_candidates():
    candidates = [place("b", "Totoya"), place("a", "Totoya")]

    assert match_mention("Totoya", candidates).place.place_id == "a"
    assert match_mention("Totoya", list(reversed(candidates))).place.place_id == "a"


@pytest.mark.parametrize("name", ["居酒屋 風鐸 FUTAKU", "FUTAKU", "futaku"])
def test_japanese_and_romanized_forms_reach_the_same_place(name):
    match = match_mention(name, [place("a", "居酒屋 風鐸 FUTAKU")])

    assert match.accepted


# --- recording the outcome ---------------------------------------------------


def test_an_accepted_match_is_recorded_on_the_mention():
    mention = MentionedEntity(name="Totoya", kind="restaurant")
    match = match_mention("Totoya", [place("a", "Totoya")])

    resolved = apply_match(mention, match, "ent_1")

    assert resolved.resolved is True
    assert resolved.entity_id == "ent_1"
    assert resolved.resolution_note


def test_a_refusal_is_recorded_with_its_reason():
    mention = MentionedEntity(name="Ghost Bar", kind="bar")
    match = match_mention("Ghost Bar", [place("a", "Sakaba Totoya")])

    unresolved = apply_match(mention, match, None)

    assert unresolved.resolved is False
    assert unresolved.entity_id is None
    assert "unresolved" in unresolved.resolution_note


def test_a_weak_match_is_not_silently_promoted():
    """An entity_id offered for a weak match must still be refused."""
    mention = MentionedEntity(name="Tokyo Ramen Ichiban Kagurazaka", kind="restaurant")
    match = match_mention(mention.name, [place("a", "Ichiban Sushi Ginza")])

    result = apply_match(mention, match, "ent_wrong")

    assert result.resolved is False
    assert result.entity_id is None
