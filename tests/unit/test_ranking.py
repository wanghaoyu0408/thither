"""Rough ranking: deterministic, explainable, and honest about missing data."""

from app.models.place import PlaceSummary
from app.services.ranking_service import RankingWeights, rank_places, score_place

SHIBUYA = (35.6595, 139.7005)


def place(place_id: str, **overrides) -> PlaceSummary:
    base = {"place_id": place_id, "name": place_id, "lat": 35.6595, "lng": 139.7005}
    return PlaceSummary(**{**base, **overrides})


def test_a_better_rating_scores_higher():
    good = score_place(place("good", rating=4.7, rating_count=1000))
    poor = score_place(place("poor", rating=3.6, rating_count=1000))

    assert good.score.total > poor.score.total


def test_review_count_breaks_a_rating_tie():
    """A 5.0 from seven people should not beat a 4.4 from thousands."""
    established = score_place(place("many", rating=4.4, rating_count=3000))
    obscure = score_place(place("few", rating=5.0, rating_count=7))

    assert established.score.total > obscure.score.total


def test_closer_places_score_higher():
    near = score_place(place("near", rating=4.3, rating_count=500), origin=SHIBUYA)
    far = score_place(
        place("far", rating=4.3, rating_count=500, lat=35.71, lng=139.80), origin=SHIBUYA
    )

    assert near.score.total > far.score.total


def test_dimensions_are_kept_so_the_score_can_be_explained():
    ranked = score_place(
        place("x", rating=4.5, rating_count=800, price_level=2),
        origin=SHIBUYA,
        target_price_level=2,
    )

    assert set(ranked.score.dimensions) == {"rating", "confidence", "proximity", "price_fit"}
    assert all(0.0 <= value <= 1.0 for value in ranked.score.dimensions.values())


def test_missing_data_is_reported_not_scored_as_zero():
    ranked = score_place(place("bare"))

    assert "rating" not in ranked.score.dimensions
    assert "no data for" in ranked.score.notes
    assert "rating" in ranked.score.notes


def test_a_missing_dimension_is_not_scored_as_zero():
    """Renormalizing means "price unknown" beats "price known to be wrong"."""
    unknown_price = score_place(place("b", rating=4.5, rating_count=800), target_price_level=2)
    badly_priced = score_place(
        place("c", rating=4.5, rating_count=800, price_level=0), target_price_level=4
    )

    assert badly_priced.score.dimensions["price_fit"] == 0.0
    assert unknown_price.score.total > badly_priced.score.total


def test_price_fit_rewards_the_target_level():
    on_target = score_place(place("a", price_level=2), target_price_level=2)
    off_target = score_place(place("b", price_level=4), target_price_level=2)

    assert on_target.score.dimensions["price_fit"] > off_target.score.dimensions["price_fit"]


def test_ranking_is_sorted_and_limited():
    ranked = rank_places(
        [
            place("mid", rating=4.2, rating_count=500),
            place("best", rating=4.8, rating_count=2000),
            place("worst", rating=3.5, rating_count=500),
        ],
        limit=2,
    )

    assert [item.place.place_id for item in ranked] == ["best", "mid"]


def test_order_is_stable_for_identical_scores():
    identical = [place("b", rating=4.4, rating_count=500), place("a", rating=4.4, rating_count=500)]

    assert [item.place.place_id for item in rank_places(identical)] == ["a", "b"]
    assert [item.place.place_id for item in rank_places(list(reversed(identical)))] == ["a", "b"]


def test_closed_places_are_filtered_out_entirely():
    ranked = rank_places(
        [
            place("open", rating=4.0, rating_count=100),
            place("shut", rating=4.9, rating_count=5000, business_status="CLOSED_PERMANENTLY"),
        ]
    )

    assert [item.place.place_id for item in ranked] == ["open"]


def test_hard_filters_disqualify_rather_than_discount():
    ranked = rank_places(
        [place("low", rating=3.9, rating_count=900), place("high", rating=4.5, rating_count=900)],
        min_rating=4.2,
    )

    assert [item.place.place_id for item in ranked] == ["high"]


def test_min_rating_count_excludes_unproven_places():
    ranked = rank_places(
        [place("new", rating=4.9, rating_count=8), place("proven", rating=4.3, rating_count=900)],
        min_rating_count=100,
    )

    assert [item.place.place_id for item in ranked] == ["proven"]


def test_places_without_a_rating_fail_a_min_rating_filter():
    assert rank_places([place("unknown")], min_rating=4.0) == []


def test_weights_are_configurable():
    only_proximity = RankingWeights(rating=0.0, confidence=0.0, proximity=1.0, price_fit=0.0)

    ranked = rank_places(
        [
            place("near_but_poor", rating=3.6, rating_count=50),
            place("far_but_great", rating=4.9, rating_count=5000, lat=35.75, lng=139.85),
        ],
        origin=SHIBUYA,
        weights=only_proximity,
    )

    assert ranked[0].place.place_id == "near_but_poor"


def test_pros_and_cons_are_generated_for_explanation():
    ranked = score_place(place("x", rating=4.6, rating_count=2500), origin=SHIBUYA)

    assert any("2,500 reviews" in pro for pro in ranked.pros)


def test_thin_review_counts_are_called_out():
    ranked = score_place(place("x", rating=4.9, rating_count=12))

    assert any("not yet reliable" in con for con in ranked.cons)


def test_empty_input_ranks_to_nothing():
    assert rank_places([]) == []
