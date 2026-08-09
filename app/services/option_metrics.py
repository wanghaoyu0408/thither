"""The objective figures behind a recommendation, in the terms they were stored.

Its own module because two callers need it and neither can import the other:
the decisions projection puts these on a comparison card, and the explanation
service puts them behind `Why?`. Both have to show the same numbers - a card and
its explanation disagreeing about a price is worse than either being absent.

Everything here reads a stored option. Nothing is recomputed, estimated, or
derived from anything but what the ranker wrote down at the time.
"""

from pydantic import BaseModel

from app.models.decision import FlightOptionData, HotelAreaOption, HotelOptionData

# Below this, a guest rating says more about who bothered to review than about
# the hotel. One home for the threshold, so a card and an explanation cannot
# disagree about what counts as thin.
THIN_REVIEWS = 50

RATING_LABELS = {
    "star_category": "Hotel category",
    "user_rating": "Guest rating",
    "location_score": "Location score",
}


class Metric(BaseModel):
    """One objective figure, as it was stored.

    `note` is where a figure declares what it is not: a drive time measured in
    a mode we did not ask for, a rating resting on three reviews.
    """

    label: str
    value: str
    note: str | None = None


def _money(value) -> str | None:
    return f"{value.amount:,.0f} {value.currency}" if value else None


def mode_note(mode: str | None) -> str | None:
    """Say when a figure was measured in a mode nobody asked for."""
    if mode == "driving":
        return "measured by car; Google publishes no transit times here"
    return None


def metrics_for(data) -> list[Metric]:
    """The objective figures this payload actually carries."""
    metrics: list[Metric] = []

    if isinstance(data, FlightOptionData):
        per_person = _money(data.price_per_person or data.price)
        if per_person:
            metrics.append(Metric(label="Price", value=f"{per_person} per person"))
        if data.stops is not None:
            metrics.append(
                Metric(label="Stops", value="Nonstop" if data.is_nonstop else f"{data.stops} stop")
            )
        if data.duration_minutes:
            hours, minutes = divmod(int(data.duration_minutes), 60)
            metrics.append(Metric(label="Duration", value=f"{hours}h {minutes:02d}m"))
        if data.airlines:
            metrics.append(Metric(label="Airline", value=", ".join(data.airlines)))
        if not data.live_mode:
            metrics.append(
                Metric(
                    label="Fare source",
                    value="provider test environment",
                    note="this is not a real price and cannot be booked",
                )
            )
        return metrics

    if isinstance(data, HotelAreaOption):
        mode = data.travel_mode or "travel"
        if data.mean_minutes is not None:
            metrics.append(
                Metric(
                    label="Mean travel to your stops",
                    value=f"{data.mean_minutes:.0f} min by {mode}",
                    note=mode_note(data.travel_mode),
                )
            )
        if data.worst_minutes is not None:
            metrics.append(Metric(label="Worst stop", value=f"{data.worst_minutes:.0f} min"))
        if data.anchor_entity_ids:
            reachable = len(data.anchor_entity_ids) - len(data.unreachable_anchors)
            metrics.append(
                Metric(
                    label="Stops reachable",
                    value=f"{reachable} of {len(data.anchor_entity_ids)}",
                    note=(
                        f"no route found to {len(data.unreachable_anchors)}"
                        if data.unreachable_anchors
                        else None
                    ),
                )
            )
        if data.community_sentiment:
            metrics.append(
                Metric(
                    label="Community view",
                    value=data.community_sentiment,
                    note=f"from {len(data.source_urls)} source(s)" if data.source_urls else None,
                )
            )
        return metrics

    if isinstance(data, HotelOptionData):
        # Star category and guest rating are separate claims from separate
        # sources, and are never combined into one figure.
        for rating in data.ratings:
            thin = rating.type == "user_rating" and (rating.review_count or 0) < THIN_REVIEWS
            metrics.append(
                Metric(
                    label=RATING_LABELS.get(rating.type, rating.type),
                    value=rating.describe(),
                    note="few enough reviews that this is weak evidence" if thin else None,
                )
            )
        nightly = _money(data.nightly_price)
        if nightly:
            metrics.append(Metric(label="Nightly", value=nightly))
        mean = data.mean_route_minutes()
        if mean is not None:
            metrics.append(
                Metric(
                    label="Mean travel to your stops",
                    value=f"{mean:.0f} min by {data.route_mode or 'travel'}",
                    note=mode_note(data.route_mode),
                )
            )
        return metrics

    return metrics


def unscored_for(data) -> list[str]:
    """What was deliberately left out of the ranking, and why.

    Named rather than omitted. From the numbers alone, "we looked and it scored
    badly" and "nobody publishes this" are indistinguishable, and only one of
    them is a reason to discount a place.
    """
    if isinstance(data, HotelAreaOption):
        return [
            "Quietness - no provider publishes a noise measure for a neighbourhood",
            "Nightlife and food density - not measured; the community view above is "
            "the only signal held for either",
        ]
    if isinstance(data, HotelOptionData):
        return [
            "Quietness - no hotel source publishes a noise measure",
            "Room size - not published, and a star category describes facilities "
            "rather than square metres",
        ]
    return []
