"""Milestone 6 acceptance.

    The agent recommends a neighbourhood first, then hotels.

Fixtures and scripted providers, deliberately. The point being proved is the
*ordering* and the reasoning behind it, and both have to hold without a network
or an API key - the same reason M5's argument was proved on fixtures rather than
on the sandbox.

The travel times are injected rather than fetched, so "this area is closer to
what the trip actually does" is a claim about the ranking and not about Tokyo's
train timetable on the day the suite happens to run.
"""

from datetime import date

import pytest

from app.models.common import LatLng, Money
from app.models.decision import (
    Decision,
    DecisionOption,
    HotelAreaOption,
    HotelOptionData,
)
from app.models.entity import PlaceEntity
from app.models.hotel import HotelPriceQuote, HotelRating, SearchHotelsInput
from app.models.itinerary import ItineraryDay, ItineraryItem, TripItinerary
from app.models.place import PlaceSummary
from app.models.route import RouteLeg
from app.models.traveler import HotelPreferences
from app.models.trip import DestinationSpec, TripBrief, TripDates, TripDecisions, TripState
from app.providers.base import ProviderUnavailable
from app.services.cache import InProcessCache, LayeredCache
from app.services.hotel_area_service import HotelAreaService, build_area_decision
from app.services.hotel_ranking import describe_prices, describe_ratings, rank_hotels
from app.services.hotel_service import HotelService
from app.services.integrity_service import check_integrity
from app.services.place_service import PlaceService
from app.services.route_service import RouteService

CHECK_IN = date(2026, 10, 3)
CHECK_OUT = date(2026, 10, 8)

# North-east Tokyo: everything this trip plans to do is out here, which is what
# makes an area near it convenient and Shinjuku not.
ANCHORS = {
    "ent_sensoji": ("Senso-ji", 35.7148, 139.7967),
    "ent_skytree": ("Tokyo Skytree", 35.7101, 139.8107),
    "ent_ueno": ("Ueno Park", 35.7148, 139.7737),
    "ent_akiba": ("Akihabara Radio Kaikan", 35.6984, 139.7731),
}


# --- fixtures and scripted providers -----------------------------------------


def anchor(entity_id: str) -> PlaceEntity:
    name, lat, lng = ANCHORS[entity_id]
    return PlaceEntity(
        entity_id=entity_id,
        name=name,
        categories=["tourist_attraction"],
        lat=lat,
        lng=lng,
        provider_refs={"google_place_id": f"place_{entity_id}"},
    )


def tokyo_trip() -> TripState:
    """A trip with real places on it, which is what an area is measured against."""
    state = TripState.new(title="Tokyo, east side")
    state.brief = TripBrief(
        destination=DestinationSpec(city="Tokyo", country="Japan", flexible=False),
        timezone="Asia/Tokyo",
        dates=TripDates(start=CHECK_IN, end=CHECK_OUT),
    )
    state.entities = {entity_id: anchor(entity_id) for entity_id in ANCHORS}
    state.itinerary = TripItinerary(
        days=[
            ItineraryDay(
                date=CHECK_IN,
                items=[
                    ItineraryItem(
                        item_id=f"item_{entity_id}",
                        type="activity",
                        entity_id=entity_id,
                        title=ANCHORS[entity_id][0],
                    )
                    for entity_id in ANCHORS
                ],
            )
        ]
    )
    return state


class ScriptedRoutes:
    """Travel minutes keyed on (origin label, destination label).

    A pair with no entry comes back unreachable rather than defaulting to
    something plausible - "no route" is a real answer and the ranker has to
    handle it.
    """

    def __init__(self, minutes: dict[tuple[str, str], float], default: float | None = None):
        self.minutes = minutes
        self.default = default
        self.calls = 0

    async def compute_route_matrix(self, origins, destinations, *, mode, departure_at=None):
        self.calls += 1
        legs = []
        for o, origin in enumerate(origins):
            for d, destination in enumerate(destinations):
                value = self.minutes.get((origin.label, destination.label), self.default)
                legs.append(
                    RouteLeg(
                        origin_index=o,
                        destination_index=d,
                        origin_label=origin.label,
                        destination_label=destination.label,
                        mode=mode,
                        duration_seconds=None if value is None else int(value * 60),
                        status="ok" if value is not None else "zero_results",
                    )
                )
        return legs


class ScriptedPlaces:
    """Text search answered from a table, with every query recorded."""

    def __init__(self, by_query: dict[str, list[PlaceSummary]] | None = None):
        self.by_query = by_query or {}
        self.queries: list[str] = []

    async def search_text(self, *, text_query, **kwargs):
        self.queries.append(text_query)
        for needle, places in self.by_query.items():
            if needle.lower() in text_query.lower():
                return list(places)
        return []

    async def get_details(self, place_id, **kwargs):
        raise AssertionError("the shortlist should not need a details call")


class FakeHotelProvider:
    name = "fake_hotels"

    def __init__(self, options: list[HotelOptionData], *, live_mode=True, raises=None):
        self.options = options
        self.live_mode = live_mode
        self.raises = raises
        self.calls: list[SearchHotelsInput] = []

    async def search_hotels(self, spec: SearchHotelsInput) -> list[HotelOptionData]:
        self.calls.append(spec)
        if self.raises:
            raise self.raises
        return [option.model_copy(deep=True) for option in self.options]


def cache() -> LayeredCache:
    return LayeredCache(InProcessCache(), None)


def area_service(routes: ScriptedRoutes, places: ScriptedPlaces | None = None) -> HotelAreaService:
    return HotelAreaService(
        PlaceService(places or ScriptedPlaces(), cache()),
        RouteService(routes, cache()),
        # No research service: the ranking must stand on travel times alone.
        None,
    )


def hotel_service(
    provider: FakeHotelProvider,
    routes: ScriptedRoutes | None = None,
    places: ScriptedPlaces | None = None,
) -> HotelService:
    return HotelService(
        provider,
        PlaceService(places or ScriptedPlaces(), cache()),
        RouteService(routes or ScriptedRoutes({}, default=15.0), cache()),
        cache(),
    )


def hotel(
    name: str,
    *,
    nightly: float | None = 200.0,
    stars: float | None = None,
    rating: float | None = None,
    reviews: int | None = None,
    quotes: list[tuple[str, float]] | None = None,
    lat: float = 35.7120,
    lng: float = 139.7960,
    area: str | None = "Asakusa",
    live_mode: bool = True,
) -> HotelOptionData:
    ratings: list[HotelRating] = []
    if stars is not None:
        ratings.append(HotelRating(value=stars, type="star_category", source="google_hotels"))
    if rating is not None:
        ratings.append(
            HotelRating(
                value=rating, type="user_rating", source="google_hotels", review_count=reviews
            )
        )

    return HotelOptionData(
        provider="fake_hotels",
        offer_ref=f"tok_{name.lower().replace(' ', '_')}",
        live_mode=live_mode,
        name=name,
        lat=lat,
        lng=lng,
        area_name=area,
        nightly_price=Money(amount=nightly) if nightly is not None else None,
        ratings=ratings,
        quotes=[
            HotelPriceQuote(source=source, nightly=Money(amount=amount))
            for source, amount in (quotes or [])
        ],
    )


# --- the acceptance ----------------------------------------------------------


async def test_the_neighbourhood_is_decided_before_any_hotel_is_searched():
    """The whole milestone in one test.

    Areas are ranked from real travel times to the trip's own anchors, the
    closest wins, and the hotel provider has not been touched at the point the
    area decision exists.
    """
    routes = ScriptedRoutes(
        {
            # Asakusa: minutes from the trip's east-side places.
            ("Asakusa", "Senso-ji"): 6.0,
            ("Asakusa", "Tokyo Skytree"): 12.0,
            ("Asakusa", "Ueno Park"): 11.0,
            ("Asakusa", "Akihabara Radio Kaikan"): 15.0,
            # Shinjuku: the other side of the city.
            ("Shinjuku", "Senso-ji"): 42.0,
            ("Shinjuku", "Tokyo Skytree"): 47.0,
            ("Shinjuku", "Ueno Park"): 33.0,
            ("Shinjuku", "Akihabara Radio Kaikan"): 29.0,
        },
        default=25.0,
    )
    places = ScriptedPlaces(
        {
            "Asakusa": [
                PlaceSummary(place_id="ChIJ_asakusa", name="Asakusa", lat=35.712, lng=139.796)
            ],
            "Shinjuku": [
                PlaceSummary(place_id="ChIJ_shinjuku", name="Shinjuku", lat=35.6938, lng=139.7034)
            ],
        }
    )
    provider = FakeHotelProvider([hotel("Asakusa View")])

    state = tokyo_trip()
    result = await area_service(routes, places).recommend_areas(
        state, suggested_areas=["Asakusa", "Shinjuku"]
    )

    assert result.ok
    names = [area.candidate.area_name for area in result.results]
    assert "Asakusa" in names and "Shinjuku" in names

    best = result.results[0]
    assert best.candidate.area_name == "Asakusa"
    assert best.mean_minutes == 11.0
    assert best.score.total > result.results[-1].score.total

    # The point of the ordering: nothing has asked a hotel provider anything.
    assert provider.calls == []

    decision = build_area_decision(result.results, select_best=True)
    state.decisions = TripDecisions(hotel_area=decision)

    # And only now does a hotel search have somewhere to look.
    search = await hotel_service(provider).search_hotels(
        SearchHotelsInput(check_in=CHECK_IN, check_out=CHECK_OUT), state=state
    )
    assert search.ok
    assert provider.calls[0].area_name == "Asakusa"


async def test_the_ranking_quotes_figures_that_came_from_the_routing_tool():
    routes = ScriptedRoutes({}, default=18.0)
    state = tokyo_trip()

    result = await area_service(routes).recommend_areas(state)

    assert result.ok
    best = result.results[0]
    assert best.mean_minutes == 18.0
    assert best.worst_minutes == 18.0
    assert best.anchor_count == len(ANCHORS)
    assert best.reachable == len(ANCHORS)
    assert "mean_travel" in best.score.dimensions
    assert "coverage" in best.score.dimensions


async def test_an_area_with_no_route_to_half_the_trip_scores_worse():
    routes = ScriptedRoutes(
        {
            ("Reachable", "Senso-ji"): 10.0,
            ("Reachable", "Tokyo Skytree"): 10.0,
            ("Reachable", "Ueno Park"): 10.0,
            ("Reachable", "Akihabara Radio Kaikan"): 10.0,
            ("Cut off", "Senso-ji"): 10.0,
            ("Cut off", "Tokyo Skytree"): 10.0,
        }
    )
    places = ScriptedPlaces(
        {
            "Reachable": [PlaceSummary(place_id="p1", name="Reachable", lat=35.712, lng=139.796)],
            "Cut off": [PlaceSummary(place_id="p2", name="Cut off", lat=35.60, lng=139.60)],
        }
    )

    result = await area_service(routes, places).recommend_areas(
        tokyo_trip(), suggested_areas=["Reachable", "Cut off"]
    )

    ranked = {area.candidate.area_name: area for area in result.results}
    assert ranked["Reachable"].score.total > ranked["Cut off"].score.total
    assert ranked["Cut off"].reachable == 2
    assert len(ranked["Cut off"].unreachable_anchors) == 2
    assert any("no route found" in con for con in ranked["Cut off"].cons)


async def test_ranking_areas_needs_somewhere_to_measure_against():
    """A trip with no places at all is a precondition failure, not an empty result."""
    empty = TripState.new(title="nothing found yet")

    result = await area_service(ScriptedRoutes({}, default=10.0)).recommend_areas(empty)

    assert not result.ok
    assert result.error.code == "invalid_request"
    assert "no places yet" in result.error.message


async def test_a_trip_with_discovered_places_but_no_itinerary_still_ranks_areas():
    """Discovery fills the registry before any day exists.

    Refusing until the trip has an itinerary would put choosing where to stay
    *after* the planning it is meant to inform - so the registry is used, and
    the weaker evidence is declared rather than hidden.
    """
    state = tokyo_trip()
    state.itinerary = TripItinerary()

    result = await area_service(ScriptedRoutes({}, default=14.0)).recommend_areas(state)

    assert result.ok
    assert result.results[0].mean_minutes == 14.0
    assert result.results[0].anchor_count == len(ANCHORS)
    assert any("nothing is scheduled or shortlisted yet" in w for w in result.warnings)


async def test_scheduled_places_are_preferred_over_merely_known_ones():
    state = tokyo_trip()
    # A place the trip knows of but has not committed to.
    state.entities["ent_far"] = PlaceEntity(
        entity_id="ent_far", name="Somewhere Else", lat=35.60, lng=139.60
    )

    result = await area_service(ScriptedRoutes({}, default=12.0)).recommend_areas(state)

    assert result.ok
    # Four scheduled anchors; the uncommitted fifth is not one of them.
    assert result.results[0].anchor_count == len(ANCHORS)
    assert "ent_far" not in result.results[0].minutes_by_anchor
    assert not any("nothing is scheduled" in w for w in result.warnings)


# --- a mode with no coverage is substituted, never disguised -----------------


async def test_transit_falls_back_to_driving_where_google_has_no_transit_data():
    """Google answers transit queries in the US and returns nothing in Japan.

    That is a licensing gap in Google's data, not a failure - so the comparison
    goes ahead by car, and says so rather than letting a driving minute be read
    as a train minute.
    """
    routes = ScriptedRoutes(
        # Transit: nothing anywhere, which is what Japan actually returns.
        {("Asakusa", name): 9.0 for name, _lat, _lng in ANCHORS.values()},
    )
    calls: list[str] = []
    original = routes.compute_route_matrix

    async def recording(origins, destinations, *, mode, departure_at=None):
        calls.append(mode)
        if mode == "transit":
            return [
                leg.model_copy(update={"duration_seconds": None, "status": "zero_results"})
                for leg in await original(
                    origins, destinations, mode=mode, departure_at=departure_at
                )
            ]
        return await original(origins, destinations, mode=mode, departure_at=departure_at)

    routes.compute_route_matrix = recording
    places = ScriptedPlaces(
        {"Asakusa": [PlaceSummary(place_id="p", name="Asakusa", lat=35.712, lng=139.796)]}
    )

    result = await area_service(routes, places).recommend_areas(
        tokyo_trip(), suggested_areas=["Asakusa"], mode="transit"
    )

    assert result.ok
    assert calls == ["transit", "driving"]

    best = next(a for a in result.results if a.candidate.area_name == "Asakusa")
    assert best.mode == "driving"
    assert best.mean_minutes == 9.0
    assert any("no transit route was found" in w and "driving times" in w for w in result.warnings)


async def test_no_route_in_any_mode_is_a_refusal_rather_than_a_ranking():
    """With nothing measured there is no comparison, whatever else is known."""
    routes = ScriptedRoutes({})  # every pair unreachable, in every mode

    result = await area_service(routes).recommend_areas(tokyo_trip(), mode="transit")

    assert not result.ok
    assert "cannot be compared" in result.error.message
    assert result.error.retryable is False
    assert result.results == []


async def test_a_few_missing_pairs_do_not_trigger_a_mode_switch():
    """Some places genuinely have no route; that is a true answer worth keeping."""
    routes = ScriptedRoutes(
        {
            ("Asakusa", "Senso-ji"): 6.0,
            ("Asakusa", "Ueno Park"): 11.0,
            # Skytree and Akihabara left unreachable.
        }
    )
    calls: list[str] = []
    original = routes.compute_route_matrix

    async def recording(origins, destinations, *, mode, departure_at=None):
        calls.append(mode)
        return await original(origins, destinations, mode=mode, departure_at=departure_at)

    routes.compute_route_matrix = recording
    places = ScriptedPlaces(
        {"Asakusa": [PlaceSummary(place_id="p", name="Asakusa", lat=35.712, lng=139.796)]}
    )

    result = await area_service(routes, places).recommend_areas(
        tokyo_trip(), suggested_areas=["Asakusa"], mode="transit"
    )

    assert result.ok
    assert calls == ["transit"]
    best = next(a for a in result.results if a.candidate.area_name == "Asakusa")
    assert best.mode == "transit"
    assert best.reachable == 2


async def test_a_hotels_travel_time_records_how_it_was_measured():
    close = hotel("Near", nightly=250.0)
    routes = ScriptedRoutes({("Near", name): 12.0 for name, _lat, _lng in ANCHORS.values()})
    places = ScriptedPlaces(
        {"Near": [PlaceSummary(place_id="p_near", name="Near Hotel", lat=35.712, lng=139.796)]}
    )

    shortlist = await hotel_service(FakeHotelProvider([close]), routes, places).shortlist(
        [close], state=tokyo_trip(), size=1, mode="driving"
    )

    option = shortlist.ranked[0].option
    assert option.route_mode == "driving"
    assert any("driving" in pro for pro in shortlist.ranked[0].pros)


# --- the ordering is enforced, not requested ---------------------------------


async def test_searching_hotels_without_an_area_refuses_and_names_the_first_step():
    provider = FakeHotelProvider([hotel("Anywhere")])

    result = await hotel_service(provider).search_hotels(
        SearchHotelsInput(check_in=CHECK_IN, check_out=CHECK_OUT), state=tokyo_trip()
    )

    assert not result.ok
    assert result.error.code == "invalid_request"
    assert "recommend_hotel_areas" in result.error.message
    # Refused before spending anything.
    assert provider.calls == []


async def test_an_explicit_area_name_is_a_resolved_area():
    provider = FakeHotelProvider([hotel("Asakusa View")])

    result = await hotel_service(provider).search_hotels(
        SearchHotelsInput(area_name="Asakusa", check_in=CHECK_IN, check_out=CHECK_OUT),
        state=tokyo_trip(),
    )

    assert result.ok
    assert provider.calls[0].query_text == "Asakusa Tokyo"


async def test_a_hotel_outside_the_selected_area_breaks_integrity():
    state = tokyo_trip()
    state.decisions = TripDecisions(
        hotel_area=Decision[HotelAreaOption](
            decision_id="dec_area",
            status="selected",
            options=[
                DecisionOption[HotelAreaOption](
                    option_id="opt_asakusa",
                    data=HotelAreaOption(
                        area_name="Asakusa", center=LatLng(lat=35.7120, lng=139.7960)
                    ),
                    status="selected",
                )
            ],
            selected_option_id="opt_asakusa",
        ),
        hotel=Decision[HotelOptionData](
            decision_id="dec_hotel",
            status="selected",
            options=[
                DecisionOption[HotelOptionData](
                    option_id="opt_far",
                    # Shinjuku: the other side of the city from the chosen area.
                    data=hotel("Park Hyatt", lat=35.6852, lng=139.6910, area="Shinjuku"),
                    status="selected",
                )
            ],
            selected_option_id="opt_far",
        ),
    )

    problems = check_integrity(state)

    assert any("Park Hyatt" in problem and "Asakusa" in problem for problem in problems)


async def test_a_hotel_inside_the_selected_area_is_fine():
    state = tokyo_trip()
    state.decisions = TripDecisions(
        hotel_area=Decision[HotelAreaOption](
            decision_id="dec_area",
            status="selected",
            options=[
                DecisionOption[HotelAreaOption](
                    option_id="opt_asakusa",
                    data=HotelAreaOption(
                        area_name="Asakusa", center=LatLng(lat=35.7120, lng=139.7960)
                    ),
                    status="selected",
                )
            ],
            selected_option_id="opt_asakusa",
        ),
        hotel=Decision[HotelOptionData](
            decision_id="dec_hotel",
            status="selected",
            options=[
                DecisionOption[HotelOptionData](
                    option_id="opt_near",
                    data=hotel("Asakusa View", lat=35.7145, lng=139.7955),
                    status="selected",
                )
            ],
            selected_option_id="opt_near",
        ),
    )

    assert check_integrity(state) == []


async def test_choosing_a_hotel_with_no_area_decision_at_all_breaks_integrity():
    state = tokyo_trip()
    state.decisions = TripDecisions(
        hotel=Decision[HotelOptionData](
            decision_id="dec_hotel",
            status="selected",
            options=[
                DecisionOption[HotelOptionData](
                    option_id="opt_any", data=hotel("Somewhere"), status="selected"
                )
            ],
            selected_option_id="opt_any",
        )
    )

    problems = check_integrity(state)

    assert any("no hotel_area is" in problem for problem in problems)


# --- the bypass is possible, but never silent --------------------------------


def test_a_bypass_without_a_reason_is_rejected_by_the_model():
    with pytest.raises(ValueError, match="bypass_reason"):
        SearchHotelsInput(check_in=CHECK_IN, check_out=CHECK_OUT, bypass_area_decision=True)


async def test_a_bypass_works_and_the_reason_is_stored_on_the_option():
    provider = FakeHotelProvider([hotel("Park Hyatt", area=None)])

    result = await hotel_service(provider).search_hotels(
        SearchHotelsInput(
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            bypass_area_decision=True,
            bypass_reason="the traveller named this hotel specifically",
        ),
        state=tokyo_trip(),
    )

    assert result.ok
    assert result.results[0].area_bypass_reason == "the traveller named this hotel specifically"
    assert any("neighbourhood step was skipped" in warning for warning in result.warnings)


async def test_a_bypassed_hotel_does_not_break_integrity():
    state = tokyo_trip()
    chosen = hotel("Park Hyatt", lat=35.6852, lng=139.6910, area="Shinjuku")
    chosen.area_bypass_reason = "the traveller named this hotel specifically"
    state.decisions = TripDecisions(
        hotel_area=Decision[HotelAreaOption](
            decision_id="dec_area",
            status="selected",
            options=[
                DecisionOption[HotelAreaOption](
                    option_id="opt_asakusa",
                    data=HotelAreaOption(
                        area_name="Asakusa", center=LatLng(lat=35.7120, lng=139.7960)
                    ),
                    status="selected",
                )
            ],
            selected_option_id="opt_asakusa",
        ),
        hotel=Decision[HotelOptionData](
            decision_id="dec_hotel",
            status="selected",
            options=[
                DecisionOption[HotelOptionData](option_id="opt_far", data=chosen, status="selected")
            ],
            selected_option_id="opt_far",
        ),
    )

    assert check_integrity(state) == []


# --- two kinds of rating, never one number -----------------------------------


def test_star_category_and_guest_rating_are_separate_dimensions():
    ranked = rank_hotels([hotel("Both", stars=4.0, rating=4.5, reviews=900)])

    dimensions = ranked[0].score.dimensions
    assert "star_category" in dimensions
    assert "user_rating" in dimensions
    assert dimensions["star_category"] != dimensions["user_rating"]


def test_a_five_star_with_no_reviews_does_not_win_on_guest_rating():
    """The conflation this milestone exists to make impossible.

    The unreviewed five-star is not scored badly on guest rating - it has no
    guest rating at all, and its star category cannot stand in for one.
    """
    unreviewed = hotel("Grand Unreviewed", stars=5.0)
    reviewed = hotel("Well Liked", stars=3.0, rating=4.6, reviews=3000)

    ranked = {item.option.name: item for item in rank_hotels([unreviewed, reviewed])}

    assert "user_rating" not in ranked["Grand Unreviewed"].score.dimensions
    assert ranked["Well Liked"].score.dimensions["user_rating"] == pytest.approx(0.92)
    assert any("no guest rating published" in con for con in ranked["Grand Unreviewed"].cons)


def test_a_handful_of_reviews_is_not_scored_at_all():
    ranked = rank_hotels([hotel("Brand New", rating=5.0, reviews=3)])

    assert "user_rating" not in ranked[0].score.dimensions
    assert any("too few to read much into" in con for con in ranked[0].cons)


def test_each_rating_is_described_in_its_own_terms():
    described = describe_ratings(hotel("Both", stars=4.0, rating=4.3, reviews=2300))

    assert described == ["4-star (Google Hotels)", "4.3/5 from 2,300 reviews (Google Hotels)"]


# --- prices are per vendor, and the vendor is named --------------------------


def test_the_cheapest_vendor_quote_is_used_and_its_source_is_kept():
    priced = hotel(
        "Multi Vendor",
        nightly=None,
        quotes=[("Booking.com", 240.0), ("Hotels.com", 212.0), ("Expedia", 259.0)],
    )
    # A provider that gives no headline rate still has to be comparable.
    priced.nightly_price = priced.cheapest_quote.nightly

    assert priced.cheapest_quote.source == "Hotels.com"
    assert describe_prices(priced) == [
        "240 USD/night at Booking.com",
        "212 USD/night at Hotels.com",
        "259 USD/night at Expedia",
    ]


def test_a_cheaper_hotel_scores_higher_on_price():
    ranked = {
        item.option.name: item
        for item in rank_hotels([hotel("Cheap", nightly=180.0), hotel("Dear", nightly=420.0)])
    }

    assert ranked["Cheap"].score.dimensions["price"] == 1.0
    assert ranked["Dear"].score.dimensions["price"] < 0.4


# --- location scoring is the point -------------------------------------------


async def test_a_close_hotel_beats_a_far_one_at_the_same_price():
    close = hotel("Near", nightly=250.0, rating=4.2, reviews=800)
    far = hotel("Far", nightly=250.0, rating=4.2, reviews=800, lat=35.6852, lng=139.6910)

    routes = ScriptedRoutes(
        {("Near", name): 10.0 for name, _lat, _lng in ANCHORS.values()}
        | {("Far", name): 40.0 for name, _lat, _lng in ANCHORS.values()}
    )
    places = ScriptedPlaces(
        {
            "Near": [PlaceSummary(place_id="p_near", name="Near Hotel", lat=35.712, lng=139.796)],
            "Far": [PlaceSummary(place_id="p_far", name="Far Hotel", lat=35.685, lng=139.691)],
        }
    )
    provider = FakeHotelProvider([close, far])
    service = hotel_service(provider, routes, places)

    search = await service.search_hotels(
        SearchHotelsInput(area_name="Asakusa", check_in=CHECK_IN, check_out=CHECK_OUT),
        state=tokyo_trip(),
    )
    shortlist = await service.shortlist(search.results, state=tokyo_trip())

    assert [item.option.name for item in shortlist.ranked] == ["Near", "Far"]
    assert shortlist.ranked[0].option.mean_route_minutes() == 10.0
    assert shortlist.ranked[1].option.mean_route_minutes() == 40.0
    assert (
        shortlist.ranked[0].score.dimensions["location"]
        > shortlist.ranked[1].score.dimensions["location"]
    )


def test_an_unrouted_hotel_has_no_location_score_rather_than_a_guessed_one():
    ranked = rank_hotels([hotel("Unmeasured", rating=4.4, reviews=500)])

    assert "location" not in ranked[0].score.dimensions
    assert "no data for" in ranked[0].score.notes
    assert any("has not been measured" in con for con in ranked[0].cons)


# --- enrichment is spent on the shortlist only -------------------------------


EIGHT_HOTELS = [
    "Sakura House",
    "Kiku Ryokan",
    "Ueno Terrace",
    "Sumida View",
    "Kappabashi Lodge",
    "Yanaka Stay",
    "Kuramae Rooms",
    "Nezu Machiya",
]


async def test_only_shortlisted_hotels_are_resolved_to_google_places():
    options = [hotel(name, nightly=200.0 + index) for index, name in enumerate(EIGHT_HOTELS)]
    places = ScriptedPlaces(
        {
            name: [
                PlaceSummary(
                    place_id=f"place_h{index}",
                    name=f"{name} Tokyo",
                    lat=35.712,
                    lng=139.796,
                    rating=4.1,
                    rating_count=400,
                )
            ]
            for index, name in enumerate(EIGHT_HOTELS)
        }
    )
    service = hotel_service(FakeHotelProvider(options), places=places)

    shortlist = await service.shortlist(options, state=tokyo_trip(), size=3)

    assert len(shortlist.ranked) == 3
    # Five of the eight were never asked about. That is the saving.
    assert len(places.queries) == 3
    assert len(shortlist.entities) == 3
    assert all(item.option.entity_id for item in shortlist.ranked)


def test_a_name_with_nothing_distinctive_in_it_is_never_matched():
    """ "Hotel 1" could be any building on the street, so it matches none of them."""
    from app.services.hotel_service import looks_like

    assert not looks_like("Hotel 1", "Hotel 1 Tokyo")
    assert looks_like("Sakura House", "Sakura House Tokyo")


async def test_a_places_result_that_is_a_different_building_is_not_attached():
    """Proximity is not identity.

    Within 500 metres of a Tokyo hotel there are several other hotels, and
    attaching the wrong one would put the wrong rating on the recommendation.
    """
    places = ScriptedPlaces(
        {
            "Asakusa": [
                PlaceSummary(
                    place_id="place_other",
                    name="Completely Different Ryokan",
                    lat=35.712,
                    lng=139.796,
                    rating=2.1,
                    rating_count=90,
                )
            ]
        }
    )
    option = hotel("Asakusa View", rating=4.4, reviews=1200)
    service = hotel_service(FakeHotelProvider([option]), places=places)

    shortlist = await service.shortlist([option], state=tokyo_trip(), size=1)

    assert shortlist.entities == []
    assert shortlist.ranked[0].option.entity_id is None
    assert shortlist.ranked[0].option.user_rating.value == 4.4
    assert any("no Google place matched" in warning for warning in shortlist.warnings)


async def test_a_disagreeing_places_rating_is_kept_beside_the_provider_one():
    places = ScriptedPlaces(
        {
            "Asakusa View": [
                PlaceSummary(
                    place_id="place_view",
                    name="Asakusa View Hotel",
                    lat=35.712,
                    lng=139.796,
                    rating=3.8,
                    rating_count=1500,
                )
            ]
        }
    )
    option = hotel("Asakusa View", rating=4.4, reviews=1200)
    service = hotel_service(FakeHotelProvider([option]), places=places)

    shortlist = await service.shortlist([option], state=tokyo_trip(), size=1)

    sources = {
        rating.source: rating.value
        for rating in shortlist.ranked[0].option.ratings
        if rating.type == "user_rating"
    }
    assert sources == {"google_hotels": 4.4, "google_places": 3.8}


# --- failure is never a hotel ------------------------------------------------


async def test_a_dead_search_is_distinguishable_from_an_empty_one():
    dead = hotel_service(
        FakeHotelProvider([], raises=ProviderUnavailable("upstream is down", "fake_hotels"))
    )
    empty = hotel_service(FakeHotelProvider([]))
    spec = SearchHotelsInput(area_name="Asakusa", check_in=CHECK_IN, check_out=CHECK_OUT)

    failed = await dead.search_hotels(spec, state=tokyo_trip())
    nothing = await empty.search_hotels(spec, state=tokyo_trip())

    assert not failed.ok
    assert failed.error.retryable
    assert failed.results == []

    assert nothing.ok
    assert nothing.found_nothing


async def test_sandbox_hotels_are_labelled_and_disclaimed():
    provider = FakeHotelProvider([hotel("Test Property")], live_mode=False)

    result = await hotel_service(provider).search_hotels(
        SearchHotelsInput(area_name="Asakusa", check_in=CHECK_IN, check_out=CHECK_OUT),
        state=tokyo_trip(),
    )

    assert result.ok
    assert any("SANDBOX DATA" in warning for warning in result.warnings)


def test_the_validator_flags_a_trip_holding_sandbox_hotels():
    from app.services.validation_service import validate_itinerary

    state = tokyo_trip()
    state.decisions = TripDecisions(
        hotel=Decision[HotelOptionData](
            decision_id="dec_hotel",
            options=[
                DecisionOption[HotelOptionData](
                    option_id="opt_test", data=hotel("Test Property", live_mode=False)
                )
            ],
        )
    )

    flagged = [
        issue for issue in validate_itinerary(state).issues if issue.type == "sandbox_hotel_data"
    ]

    assert len(flagged) == 1
    assert "not real" in flagged[0].message


# --- the decision reaches TripState through the patch engine -----------------


async def test_the_area_decision_is_stored_as_its_own_decision(session):
    """Spec section 25: "Store hotel_area as a separate Decision. This is important."

    Through the real patch pipeline, not by assignment - a decision that cannot
    survive revision matching, locks and integrity is not stored, it is staged.
    """
    from app.db.repository import TripRepository
    from app.models.patch import TripPatch

    repository = TripRepository(session)
    stored = await repository.create(tokyo_trip())

    result = await area_service(ScriptedRoutes({}, default=13.0)).recommend_areas(stored)
    assert result.ok

    decision = build_area_decision(result.results, select_best=True)
    applied = await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="recommend where to stay",
            actor="agent",
            operations=[
                {
                    "op": "add",
                    "path": "/decisions/hotel_area",
                    "value": decision.model_dump(mode="json"),
                }
            ],
        ),
    )

    assert applied.applied, applied.errors
    saved = applied.state.decisions.hotel_area
    assert saved is not None
    assert saved.status == "selected"
    assert saved.selected_option_id == decision.options[0].option_id

    chosen = next(o for o in saved.options if o.option_id == saved.selected_option_id)
    assert chosen.data.center is not None
    # The figures behind the ranking travel with it.
    assert "13 min mean" in chosen.data.notes
    assert chosen.score.dimensions["mean_travel"] > 0


async def test_a_hotel_selected_outside_the_stored_area_is_rejected_by_the_patch_engine(session):
    """Integrity is not advisory: the patch simply does not apply."""
    from app.db.repository import TripRepository
    from app.models.patch import TripPatch

    repository = TripRepository(session)
    state = tokyo_trip()
    state.decisions = TripDecisions(
        hotel_area=Decision[HotelAreaOption](
            decision_id="dec_area",
            status="selected",
            options=[
                DecisionOption[HotelAreaOption](
                    option_id="opt_asakusa",
                    data=HotelAreaOption(
                        area_name="Asakusa", center=LatLng(lat=35.7120, lng=139.7960)
                    ),
                    status="selected",
                )
            ],
            selected_option_id="opt_asakusa",
        )
    )
    stored = await repository.create(state)

    far = Decision[HotelOptionData](
        decision_id="dec_hotel",
        status="selected",
        options=[
            DecisionOption[HotelOptionData](
                option_id="opt_far",
                data=hotel("Park Hyatt", lat=35.6852, lng=139.6910, area="Shinjuku"),
                status="selected",
            )
        ],
        selected_option_id="opt_far",
    )

    applied = await repository.apply_patch(
        stored.trip_id,
        TripPatch(
            base_revision=stored.revision,
            reason="book the one I like",
            actor="agent",
            operations=[
                {"op": "add", "path": "/decisions/hotel", "value": far.model_dump(mode="json")}
            ],
        ),
    )

    assert not applied.applied
    assert any(error.code == "INTEGRITY_ERROR" for error in applied.errors)


# --- the agent's own path ----------------------------------------------------


class FakeToolbox:
    def __init__(self, hotel_areas, hotels=None):
        self.hotel_areas = hotel_areas
        self.hotels = hotels


def tool_context(toolbox, state=None):
    from app.agent.tool_registry import ToolContext
    from app.config import Settings
    from app.services.proposal_store import ProposalStore

    return ToolContext(
        state=state or tokyo_trip(),
        toolbox=toolbox,
        proposals=ProposalStore(),
        settings=Settings(),
    )


async def test_the_agent_gets_the_areas_and_a_decision_to_commit():
    from app.agent.tool_registry import _apply_trip_patch, _recommend_hotel_areas

    context = tool_context(FakeToolbox(area_service(ScriptedRoutes({}, default=16.0))))

    reply = await _recommend_hotel_areas(context, {})

    assert reply["areas"], reply
    assert reply["areas"][0]["mean_minutes_to_anchors"] == 16.0
    assert reply["areas"][0]["mode"] == "transit"
    # Ranked, not chosen: the traveller has not seen this yet.
    assert reply["selected"] is False
    assert "hotel_area" in context.pending_decisions

    # And it commits without an itinerary proposal, because there are no days
    # involved in deciding where to stay.
    committed = await _apply_trip_patch(context, {"reason": "where to stay"})
    operations = committed["__patches__"][0]["operations"]

    assert [op["path"] for op in operations] == ["/decisions/hotel_area"]
    assert operations[0]["op"] == "add"


async def test_apply_still_refuses_when_there_is_nothing_staged():
    from app.agent.tool_registry import _apply_trip_patch

    context = tool_context(FakeToolbox(area_service(ScriptedRoutes({}, default=16.0))))

    result = await _apply_trip_patch(context, {"proposal_id": "nope", "reason": "x"})

    assert result["applied"] is False
    assert "no such proposal" in result["error"]


async def test_searching_hotels_without_a_provider_says_so_and_points_at_the_area_tool():
    from app.agent.tool_registry import _search_hotels

    context = tool_context(FakeToolbox(area_service(ScriptedRoutes({}, default=16.0))))

    result = await _search_hotels(
        context, {"check_in": CHECK_IN.isoformat(), "check_out": CHECK_OUT.isoformat()}
    )

    assert "SERPAPI_API_KEY" in result["error"]
    assert "recommend_hotel_areas still works" in result["hint"]


def test_both_hotel_tools_are_registered_with_the_agent():
    from app.agent.tool_registry import HANDLERS, TOOL_SCHEMAS

    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert {"recommend_hotel_areas", "search_hotels"} <= names
    assert {"recommend_hotel_areas", "search_hotels"} <= set(HANDLERS)

    hotels = next(s for s in TOOL_SCHEMAS if s["name"] == "search_hotels")
    # The rule the model must not be free to forget.
    assert "Requires an area" in hotels["description"]
    assert "never be merged" in hotels["description"]


# --- what cannot be scored is said, not invented -----------------------------


def test_quietness_and_room_size_are_named_as_gaps_rather_than_scored():
    from app.services.hotel_ranking import unscored_preferences

    notes = unscored_preferences(HotelPreferences(quiet_importance=0.9, room_size_importance=0.8))

    assert any("quietness is not part of this ranking" in note for note in notes)
    assert any("room size is not part of this ranking" in note for note in notes)

    ranked = rank_hotels(
        [hotel("Anywhere", stars=4.0, rating=4.4, reviews=800)],
        preferences=HotelPreferences(quiet_importance=0.9),
    )
    assert "quiet" not in ranked[0].score.dimensions


def test_a_stated_minimum_rating_filters_rather_than_discounts():
    """The M5 lesson, applied to hotels.

    A traveller who names a floor has told us something a thirty-dollar saving
    should not be able to overrule.
    """
    cheap_and_poor = hotel("Cheap", nightly=120.0, rating=3.1, reviews=800)
    dearer_and_good = hotel("Good", nightly=260.0, rating=4.5, reviews=800)

    ranked = rank_hotels(
        [cheap_and_poor, dearer_and_good], preferences=HotelPreferences(min_rating=4.0)
    )

    assert [item.option.name for item in ranked] == ["Good"]


def test_a_floor_that_would_leave_nothing_returns_the_options_with_the_objection():
    ranked = rank_hotels(
        [hotel("Only Option", nightly=120.0, rating=3.1, reviews=800)],
        preferences=HotelPreferences(min_rating=4.5),
    )

    assert len(ranked) == 1
    assert any("below the 4.5 rating" in con for con in ranked[0].cons)
