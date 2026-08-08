"""What a hotel price source has to offer (spec sections 3 and 16).

The spec named Amadeus. Amadeus Self-Service was decommissioned on 17 July
2026, which is the argument for this file made for us: the provider went away
and the cost was one class, because nothing above this line knew its name.

The interface is deliberately one method. Providers differ in shape - SerpApi
returns properties and prices in a single call and paginates; Amadeus needed a
hotel-id lookup and then an offers call; Booking.com Demand API wants a
destination id first. Baking either shape into the interface would make the
next swap expensive again, so pagination and multi-step lookups stay private to
the implementation.

**No provider under this interface will ever gain a booking method.** Spec
section 43 puts booking, payment and reservations out of scope, and the way to
keep them out of scope is to have nowhere to put them.

Future providers, behind this same interface:
  - Booking.com Demand API - needs a partner account; richer availability.
  - Google Hotels via a direct partner integration, if one becomes available.
"""

from typing import Protocol, runtime_checkable

from app.models.decision import HotelOptionData
from app.models.hotel import SearchHotelsInput


@runtime_checkable
class HotelProvider(Protocol):
    """A source of priced hotels. Search only."""

    name: str

    # False when the data is a test environment's. Hotels have the same rule
    # flights do: a fabricated price shown as a real one is the failure this
    # project exists to avoid, so the flag travels onto every option.
    live_mode: bool

    async def search_hotels(self, spec: SearchHotelsInput) -> list[HotelOptionData]:
        """Priced properties for one area and one set of dates.

        Raises `ProviderError` when the call fails. Returns an empty list when
        the call worked and there was simply nothing there - the two are never
        allowed to look alike.
        """
        ...
