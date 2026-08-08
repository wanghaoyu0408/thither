"""Duffel flight search (spec sections 15 and 35).

Search only. **This provider will never create an order, take a payment, or
select a seat.** Duffel offers all three; spec section 43 puts every one of them
out of scope, and the way to keep them out of scope is to not write the code.

Two shapes worth knowing before reading on:

- A slice carries exactly one origin and one destination, so comparing three
  Bay Area airports means three offer requests. The fan-out lives in
  flight_service; this file handles one request at a time.
- Nothing reports a stop count. It is the connections between segments plus any
  technical stops within them.

`live_mode` is derived here and travels with the offer from this point on. A
token beginning `duffel_test_` produces sandbox data: plausible-looking fares
for flights that do not exist. Presenting one as real would be the precise
failure this project is built to avoid, so the flag is not optional anywhere
downstream.
"""

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.models.common import Money, utcnow
from app.models.decision import FlightOptionData
from app.models.flight import BaggageAllowance, FlightSegment, FlightSlice, SearchFlightsInput
from app.providers.base import request_json

PROVIDER = "duffel"
BASE_URL = "https://api.duffel.com"
API_VERSION = "v2"

TEST_TOKEN_PREFIX = "duffel_test_"

_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def is_test_token(token: str) -> bool:
    return token.strip().startswith(TEST_TOKEN_PREFIX)


def parse_duration(value: str | None) -> int | None:
    """ISO 8601 duration to minutes. Duffel writes them like `PT02H26M`."""
    if not value:
        return None
    match = _DURATION.match(value.strip())
    if not match:
        return None
    parts = {key: int(number) for key, number in match.groupdict(default="0").items()}
    total = parts["days"] * 1440 + parts["hours"] * 60 + parts["minutes"]
    if parts["seconds"] >= 30:
        total += 1
    return total


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _search_url(origin: str, destination: str, departure: str, ret: str | None) -> str:
    """A place to go and look, not a place to buy.

    Duffel has no consumer storefront, so linking anywhere implying "book here"
    would misrepresent what the system does.
    """
    leg = f"{origin}.{destination}.{departure}"
    if ret:
        leg = f"{leg}*{destination}.{origin}.{ret}"
    return f"https://www.google.com/travel/flights?q={quote(f'flights {leg}')}"


def _baggage(offer: dict[str, Any]) -> BaggageAllowance | None:
    """Allowances, read from the first segment that states any.

    Absent means the airline did not say, which is not the same as zero.
    """
    for slice_ in offer.get("slices") or []:
        for segment in slice_.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                bags = passenger.get("baggages") or []
                if not bags:
                    continue
                counts = {"checked": None, "carry_on": None}
                for bag in bags:
                    kind = bag.get("type")
                    if kind in counts:
                        counts[kind] = bag.get("quantity")
                if counts["checked"] is not None or counts["carry_on"] is not None:
                    return BaggageAllowance(checked=counts["checked"], cabin=counts["carry_on"])
    return None


def _segment(raw: dict[str, Any]) -> FlightSegment | None:
    origin = (raw.get("origin") or {}).get("iata_code")
    destination = (raw.get("destination") or {}).get("iata_code")
    departing = _timestamp(raw.get("departing_at"))
    arriving = _timestamp(raw.get("arriving_at"))
    if not (origin and destination and departing and arriving):
        return None

    marketing = raw.get("marketing_carrier") or {}
    operating = raw.get("operating_carrier") or {}

    return FlightSegment(
        origin=origin,
        destination=destination,
        departing_at=departing,
        arriving_at=arriving,
        marketing_carrier=marketing.get("iata_code") or "??",
        marketing_carrier_name=marketing.get("name"),
        operating_carrier=operating.get("iata_code"),
        flight_number=raw.get("marketing_carrier_flight_number"),
        aircraft=(raw.get("aircraft") or {}).get("name"),
        duration_minutes=parse_duration(raw.get("duration")),
        technical_stops=len(raw.get("stops") or []),
    )


def _slice(raw: dict[str, Any]) -> FlightSlice | None:
    segments = [segment for segment in (_segment(s) for s in raw.get("segments") or []) if segment]
    if not segments:
        return None

    origin = (raw.get("origin") or {}).get("iata_code") or segments[0].origin
    destination = (raw.get("destination") or {}).get("iata_code") or segments[-1].destination

    return FlightSlice(
        origin=origin,
        destination=destination,
        departing_at=segments[0].departing_at,
        arriving_at=segments[-1].arriving_at,
        duration_minutes=parse_duration(raw.get("duration")),
        segments=segments,
    )


def normalize_offer(
    raw: dict[str, Any], *, live_mode: bool, passengers: int, currency: str
) -> FlightOptionData | None:
    """A Duffel offer in this application's shape, or None if unusable."""
    slices = [s for s in (_slice(item) for item in raw.get("slices") or []) if s]
    if not slices:
        return None

    try:
        total = float(raw.get("total_amount") or 0.0)
    except (TypeError, ValueError):
        return None

    offer_currency = raw.get("total_currency") or currency
    per_person = total / passengers if passengers else total

    conditions = raw.get("conditions") or {}
    refund = conditions.get("refund_before_departure") or {}
    change = conditions.get("change_before_departure") or {}

    airlines = list(
        dict.fromkeys(segment.marketing_carrier for item in slices for segment in item.segments)
    )

    return FlightOptionData(
        provider=PROVIDER,
        offer_ref=raw.get("id") or "",
        # Cross-checked: the token decides, and the offer's own flag can only
        # confirm. If either says sandbox, it is sandbox.
        live_mode=live_mode and bool(raw.get("live_mode", live_mode)),
        price=Money(amount=round(total, 2), currency=offer_currency),
        price_per_person=Money(amount=round(per_person, 2), currency=offer_currency),
        origin=slices[0].origin,
        destination=slices[0].destination,
        slices=slices,
        departure_at=slices[0].departing_at,
        arrival_at=slices[-1].arriving_at,
        duration_minutes=sum(s.duration_minutes or 0 for s in slices) or None,
        stops=max(s.stops for s in slices),
        airlines=airlines,
        cabin=_cabin_of(raw) or "economy",
        baggage=_baggage(raw),
        refundable=refund.get("allowed"),
        changeable=change.get("allowed"),
        search_url=_search_url(
            slices[0].origin,
            slices[0].destination,
            slices[0].departing_at.date().isoformat(),
            slices[1].departing_at.date().isoformat() if len(slices) > 1 else None,
        ),
        observed_at=utcnow(),
        expires_at=_timestamp(raw.get("expires_at")),
    )


def _cabin_of(raw: dict[str, Any]) -> str | None:
    for slice_ in raw.get("slices") or []:
        for segment in slice_.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                cabin = passenger.get("cabin_class")
                if cabin in ("economy", "premium_economy", "business", "first"):
                    # The application has no `first`; treat it as business.
                    return "business" if cabin == "first" else cabin
    return None


class DuffelProvider:
    """Flight search. Deliberately has no method that could book anything."""

    def __init__(self, access_token: str, client: httpx.AsyncClient) -> None:
        self._token = access_token
        self._client = client
        self.live_mode = not is_test_token(access_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Duffel-Version": API_VERSION,
            "Authorization": f"Bearer {self._token}",
        }

    async def search_offers(
        self,
        *,
        origin: str,
        destination: str,
        spec: SearchFlightsInput,
    ) -> list[FlightOptionData]:
        """One origin-destination pair. The caller fans out across pairs."""
        slices: list[dict[str, Any]] = [
            {
                "origin": origin,
                "destination": destination,
                "departure_date": spec.departure_date.isoformat(),
            }
        ]
        if spec.return_date:
            slices.append(
                {
                    "origin": destination,
                    "destination": origin,
                    "departure_date": spec.return_date.isoformat(),
                }
            )

        passengers = [{"type": "adult"} for _ in range(spec.adults)]
        passengers += [{"age": 8} for _ in range(spec.children)]

        body: dict[str, Any] = {
            "data": {
                "slices": slices,
                "passengers": passengers,
                "cabin_class": spec.cabin,
            }
        }
        if spec.max_stops is not None:
            body["data"]["max_connections"] = min(spec.max_stops, 2)

        payload = await request_json(
            self._client,
            "POST",
            f"{BASE_URL}/air/offer_requests",
            provider=PROVIDER,
            headers=self._headers(),
            json_body=body,
            params={"return_offers": "true"},
        )

        offers = ((payload or {}).get("data") or {}).get("offers") or []
        normalized = [
            normalize_offer(
                offer,
                live_mode=self.live_mode,
                passengers=spec.passengers,
                currency=spec.currency,
            )
            for offer in offers
        ]
        return [offer for offer in normalized if offer is not None]
