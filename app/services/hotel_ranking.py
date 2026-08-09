"""Ranking hotels, and saying why (spec sections 23 and 30).

`HotelPreferences` has carried `location_importance`, `price_importance`,
`room_size_importance`, `quiet_importance` and `min_rating` since M1 and goes
unused until now. Two of those five turn out to have no data behind them, and
this module says so rather than quietly scoring them:

**Quietness is not scored.** No provider publishes a noise measure. A traveller
who said they care is told the ranking could not account for it, which is
information; a fabricated quietness number would be the false precision the
whole evidence layer exists to prevent.

**Room size is not scored either.** Same reason. A star category is not a proxy
for it - four stars describes the facilities, not the square metres - and using
one as the other is the same conflation this milestone is built to make
impossible.

That conflation is the reason `star_category` and `user_rating` are separate
dimensions here. Folding them into one "rating" number is how a five-star with
no reviews outranks a 4.6 from three thousand people, and how "highly rated"
comes to mean "expensive category" without anyone deciding that it should.
"""

from dataclasses import dataclass, field

from app.models.decision import DecisionScore, HotelOptionData
from app.models.hotel import HotelRating
from app.models.traveler import HotelPreferences
from app.services.scoring import combine, price_score

# Beyond this a hotel is a commute from the trip, not a base for it. Same
# ceiling the area ranker uses, and for the same reason.
TRAVEL_CEILING_MINUTES = 45.0

# Below this, an average says more about who happened to review than about the
# hotel. Not scored rather than scored badly.
MIN_REVIEWS_TO_SCORE = 20

# Weights for the dimensions HotelPreferences has no field for. Fixed and
# stated, rather than borrowed from a preference that means something else.
STAR_WEIGHT = 0.4
USER_RATING_WEIGHT = 0.5


@dataclass
class RankedHotel:
    option: HotelOptionData
    score: DecisionScore
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)

    @property
    def nightly(self) -> float | None:
        return self.option.nightly_price.amount if self.option.nightly_price else None

    @property
    def currency(self) -> str:
        return self.option.nightly_price.currency if self.option.nightly_price else "USD"

    @property
    def ref(self) -> str:
        return self.option.offer_ref or self.option.name


def _price_of(option: HotelOptionData) -> float | None:
    if option.nightly_price:
        return option.nightly_price.amount
    cheapest = option.cheapest_quote
    return cheapest.nightly.amount if cheapest and cheapest.nightly else None


def _location_score(option: HotelOptionData) -> float | None:
    """Real travel minutes to this trip's anchors, or nothing at all.

    Deliberately does not fall back to `location_score` ratings or to
    straight-line distance. A hotel that has not been route-scored yet is
    unknown on this dimension, and unknown is what it should look like.
    """
    mean = option.mean_route_minutes()
    if mean is None:
        return None
    return round(max(0.0, 1.0 - mean / TRAVEL_CEILING_MINUTES), 4)


def _user_rating_score(option: HotelOptionData) -> float | None:
    rating = option.user_rating
    if rating is None or rating.scale_max <= 0:
        return None
    if rating.review_count is not None and rating.review_count < MIN_REVIEWS_TO_SCORE:
        return None
    return round(rating.value / rating.scale_max, 4)


def _star_score(option: HotelOptionData) -> float | None:
    rating = option.star_category
    if rating is None or rating.scale_max <= 0:
        return None
    return round(rating.value / rating.scale_max, 4)


def _minimum_score(option: HotelOptionData, preferences: HotelPreferences) -> float | None:
    """Whether this clears the rating floor the traveller asked for.

    `None` when they asked for nothing, or when the rating is not solid enough
    to answer with. A 5.0 from three reviews is already too thin to score on
    (`_user_rating_score` refuses it), and letting it clear a stated 4.5 floor
    anyway would be the same number being untrustworthy and authoritative at
    once. An unproven hotel has not failed the test - nobody has run it.
    """
    if preferences.min_rating is None:
        return None
    rating = option.user_rating
    if rating is None:
        return None
    if rating.review_count is not None and rating.review_count < MIN_REVIEWS_TO_SCORE:
        return None
    return 1.0 if rating.value >= preferences.min_rating else 0.0


def meets_min_rating(option: HotelOptionData, minimum: float | None) -> bool:
    """Whether the guest rating clears an explicitly stated floor.

    A hotel with no user rating is not treated as failing: absent is not low,
    and dropping every unreviewed property would quietly delete new hotels.
    """
    if minimum is None:
        return True
    rating = option.user_rating
    return rating is None or rating.value >= minimum


def score_hotel(
    option: HotelOptionData,
    *,
    cheapest: float | None,
    preferences: HotelPreferences | None = None,
) -> RankedHotel:
    preferences = preferences or HotelPreferences()

    price = _price_of(option)
    price_dimension = (
        price_score(price, cheapest) if price is not None and cheapest is not None else None
    )

    components: dict[str, tuple[float | None, float]] = {
        # A stated floor, expressed as a score rather than only as a filter.
        #
        # For one traveller `rank_hotels` filters on it, and that stays true.
        # But a filter cannot be applied per-person inside a group: one member's
        # four-star floor would silently delete every hotel the other three
        # liked. Scoring it instead keeps the requirement doing something -
        # a hotel below Dee's floor tanks *Dee's* score, which is what makes her
        # the worst-served traveller and gets her named, rather than her stated
        # requirement quietly counting for nothing.
        "meets_stated_minimum": (_minimum_score(option, preferences), 0.8),
        "price": (price_dimension, preferences.price_importance),
        # The point of the milestone: how far this hotel is from what the trip
        # actually does, in minutes a routing API measured.
        "location": (_location_score(option), preferences.location_importance),
        # Two ratings, two dimensions, never averaged into one.
        "user_rating": (_user_rating_score(option), USER_RATING_WEIGHT),
        "star_category": (_star_score(option), STAR_WEIGHT),
    }

    return RankedHotel(
        option=option,
        score=combine(components),
        pros=_pros(option),
        cons=_cons(option, preferences),
    )


def _pros(option: HotelOptionData) -> list[str]:
    pros: list[str] = []

    mean = option.mean_route_minutes()
    if mean is not None and mean <= 20.0:
        # The mode is named, never assumed: the figure may be a drive where
        # transit was asked for and unavailable.
        pros.append(f"{mean:.0f} min average {option.route_mode or 'travel'} to this trip's places")

    rating = option.user_rating
    if rating and rating.value >= 4.3:
        pros.append(rating.describe())

    stars = option.star_category
    if stars and stars.value >= 4:
        pros.append(stars.describe())

    if len(option.quotes) > 1:
        pros.append(f"priced by {len(option.quotes)} booking sites")
    if option.refundable:
        pros.append("refundable")

    return pros


def _cons(option: HotelOptionData, preferences: HotelPreferences) -> list[str]:
    cons: list[str] = []

    mean = option.mean_route_minutes()
    if mean is not None and mean > TRAVEL_CEILING_MINUTES:
        cons.append(f"{mean:.0f} min average {option.route_mode or 'travel'} to this trip's places")
    elif mean is None:
        cons.append("travel time to this trip's places has not been measured")

    rating = option.user_rating
    if rating is None:
        cons.append("no guest rating published")
    elif rating.review_count is not None and rating.review_count < MIN_REVIEWS_TO_SCORE:
        cons.append(f"only {rating.review_count} review(s), too few to read much into")

    if preferences.min_rating is not None and not meets_min_rating(option, preferences.min_rating):
        cons.append(f"below the {preferences.min_rating:g} rating this traveller asked for")

    if not option.nightly_price and not option.quotes:
        cons.append("no price available")

    gap = option.headline_gap()
    if gap is not None and gap > 0:
        # An advertised rate no listed site matches is a real difference the
        # traveller would otherwise meet at checkout.
        cons.append(
            f"{gap:.0f} {option.nightly_price.currency} above the advertised rate, which no "
            f"named booking site matched"
        )

    return cons


def unscored_preferences(preferences: HotelPreferences) -> list[str]:
    """Preferences the traveller stated that no data could answer.

    Returned so the caller can say it out loud. A ranking that silently ignores
    what someone asked for is worse than one that admits the gap - the user has
    no way to tell the difference from the numbers.
    """
    notes: list[str] = []
    if preferences.quiet_importance >= 0.6:
        notes.append(
            "quietness is not part of this ranking: no hotel source publishes a noise measure"
        )
    if preferences.room_size_importance >= 0.6:
        notes.append(
            "room size is not part of this ranking: it is not published, and a star category "
            "describes facilities rather than square metres"
        )
    return notes


def rank_hotels(
    options: list[HotelOptionData],
    *,
    preferences: HotelPreferences | None = None,
    limit: int | None = None,
    cheapest: float | None = None,
) -> list[RankedHotel]:
    """Score and sort. Ties break on the offer reference so order is stable.

    An explicit `min_rating` is a filter, not a discount - the same rule the
    flight ranker applies to avoided airlines, and for the same reason: a
    traveller who names a floor has told us something that thirty dollars
    should not be able to overrule. If the floor would leave nothing, the
    options come back with the objection stated in their cons rather than the
    traveller being told there are no hotels.

    `cheapest` may be passed in so a re-rank of the shortlist keeps scoring
    against the full search's cheapest rate. Recomputing it over the survivors
    would make the shortlist's own cheapest look like a bargain, which is a
    different claim than the one the first ranking made.
    """
    if not options:
        return []

    preferences = preferences or HotelPreferences()

    acceptable = [option for option in options if meets_min_rating(option, preferences.min_rating)]
    if acceptable:
        options = acceptable

    if cheapest is None:
        prices = [price for price in (_price_of(option) for option in options) if price is not None]
        cheapest = min(prices) if prices else None

    ranked = [score_hotel(option, cheapest=cheapest, preferences=preferences) for option in options]
    ranked.sort(
        key=lambda item: (
            -item.score.total,
            item.nightly if item.nightly is not None else 1e9,
            item.ref,
        )
    )
    return ranked[:limit] if limit else ranked


# --- explanation -------------------------------------------------------------


def describe_ratings(option: HotelOptionData) -> list[str]:
    """Every rating, each said in its own terms.

    The single rendering point for the rule this milestone is built around: a
    star category and a guest score are printed as different claims, from named
    sources, and are never combined into one figure.
    """
    return [rating.describe() for rating in option.ratings]


def describe_prices(option: HotelOptionData) -> list[str]:
    """Each vendor's rate with the vendor named.

    Google Hotels lists one property at several sites at different prices, so
    "the price" does not exist and is never printed as though it did.
    """
    lines: list[str] = []
    for quote in option.quotes:
        if quote.nightly is None:
            continue
        lines.append(f"{quote.nightly.amount:.0f} {quote.nightly.currency}/night at {quote.source}")
    return lines


# A price gap smaller than this, proportionally, is not a reason to prefer one
# hotel over another - it is noise the ranking happened to order.
NEGLIGIBLE_PRICE_FRACTION = 0.03

# One rating backed by this many times more reviews than another is worth
# mentioning. Not scored - it says how firm a number is, not how good.
REVIEW_DEPTH_RATIO = 2.0


@dataclass
class HotelTradeOff:
    """One comparison, in figures that came from a tool."""

    recommended_ref: str
    alternative_ref: str

    price_difference: float | None
    currency: str
    minutes_difference: float | None

    statements: list[str] = field(default_factory=list)
    live_mode: bool = True
    # True when nothing measured separates these two meaningfully. Presenting a
    # winner anyway would dress up a rounding difference as a recommendation.
    close_call: bool = False


def explain_hotel_choice(recommended: RankedHotel, alternative: RankedHotel) -> HotelTradeOff:
    """Why this hotel over that one, without merging what should stay apart."""
    statements: list[str] = []

    price_gap: float | None = None
    if recommended.nightly is not None and alternative.nightly is not None:
        price_gap = round(recommended.nightly - alternative.nightly, 2)
        if price_gap > 0:
            statements.append(f"{price_gap:.0f} {recommended.currency} more per night")
        elif price_gap < 0:
            statements.append(f"{abs(price_gap):.0f} {recommended.currency} less per night")
        else:
            statements.append("the same nightly price")

    minutes_gap: float | None = None
    here = recommended.option.mean_route_minutes()
    there = alternative.option.mean_route_minutes()
    # Only comparable when both were measured the same way.
    if (
        here is not None
        and there is not None
        and (recommended.option.route_mode == alternative.option.route_mode)
    ):
        minutes_gap = round(there - here, 1)
        if abs(minutes_gap) >= 3:
            direction = "closer to" if minutes_gap > 0 else "further from"
            mode = recommended.option.route_mode or "travel"
            statements.append(
                f"{abs(minutes_gap):.0f} min {direction} this trip's places by {mode} "
                f"({here:.0f} min against {there:.0f})"
            )

    substantive = 0
    for kind in ("user_rating", "star_category"):
        substantive += _compare_rating(recommended, alternative, kind, statements)

    substantive += _compare_review_depth(recommended, alternative, statements)

    if minutes_gap is not None and abs(minutes_gap) >= 3:
        substantive += 1

    # A price difference only counts as a reason when it is one.
    if price_gap is not None and recommended.nightly:
        if abs(price_gap) / max(recommended.nightly, 1.0) >= NEGLIGIBLE_PRICE_FRACTION:
            substantive += 1

    return HotelTradeOff(
        recommended_ref=recommended.ref,
        alternative_ref=alternative.ref,
        price_difference=price_gap,
        currency=recommended.currency,
        minutes_difference=minutes_gap,
        statements=statements,
        live_mode=recommended.option.live_mode and alternative.option.live_mode,
        close_call=substantive == 0,
    )


def _compare_review_depth(
    recommended: RankedHotel, alternative: RankedHotel, statements: list[str]
) -> int:
    """How firmly each rating is held, when the ratings themselves agree.

    Two hotels at 3.9/5 are not equally well described if one has 1,762 reviews
    and the other 417. This is stated rather than scored: it says how much to
    trust a number, which is not the same as the number being better.
    """
    mine = recommended.option.user_rating
    theirs = alternative.option.user_rating
    if mine is None or theirs is None or not mine.review_count or not theirs.review_count:
        return 0
    if abs(mine.value - theirs.value) >= 0.1:
        # The ratings differ; _compare_rating has already said so.
        return 0

    if mine.review_count >= theirs.review_count * REVIEW_DEPTH_RATIO:
        statements.append(
            f"the same {mine.value:g}/{mine.scale_max:g}, but from {mine.review_count:,} "
            f"reviews rather than {theirs.review_count:,}"
        )
        return 1
    if theirs.review_count >= mine.review_count * REVIEW_DEPTH_RATIO:
        statements.append(
            f"the same {mine.value:g}/{mine.scale_max:g}, but from only "
            f"{mine.review_count:,} reviews against {theirs.review_count:,}"
        )
    return 0


def _compare_rating(
    recommended: RankedHotel, alternative: RankedHotel, kind: str, statements: list[str]
) -> int:
    """Compare like with like, or not at all.

    Only ever compares a rating against the same *kind* of rating from the same
    scale. A star category against a guest score would be the one comparison
    this whole milestone exists to prevent.
    """
    mine: HotelRating | None = recommended.option.rating_of(kind)
    theirs: HotelRating | None = alternative.option.rating_of(kind)
    if mine is None or theirs is None or mine.scale_max != theirs.scale_max:
        return 0
    if abs(mine.value - theirs.value) < 0.1:
        return 0
    better = "higher" if mine.value > theirs.value else "lower"
    label = kind.replace("_", " ")
    statements.append(f"{better} {label}: {mine.describe()} vs {theirs.describe()}")
    return 1
