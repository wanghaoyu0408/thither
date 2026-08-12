"""How close this system's own numbers have run.

One endpoint, and it is not trip-scoped: the corpus is a record of providers,
not of trips or of people. Nothing here can answer "where has this person
been" - the rows hold no trip id and nothing finer than a region, which is the
whole reason they are allowed to outlive the trips that produced them.

Read-only. Outcomes are written by the reflection card and by the end-of-turn
sweep over what is self-checkable; nothing decides it was right about itself.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.repository import CalibrationRepository
from app.db.session import get_session
from app.models.calibration import DIMENSIONS, Dimension
from app.services.calibration_service import (
    Calibrations,
    dimensions_never_checked,
    evidence_line,
)

router = APIRouter(tags=["calibration"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Which keys are worth reporting per dimension. Travel time is split by mode
# because a driving estimate and a transit estimate are different
# measurements - the distinction the whole exercise exists to keep.
_KEYS: list[tuple[str, Dimension, str | None]] = [
    ("google_routes", "travel_minutes", "transit"),
    ("google_routes", "travel_minutes", "driving"),
    ("serpapi_google_hotels", "hotel_headline_gap", None),
    ("google_places", "hours_shelf_life", None),
    ("google_weather", "day_high_c", None),
]


@router.get("/calibration")
async def get_calibration(session: SessionDep, scope: str = "unknown") -> dict[str, Any]:
    """Every key, including the ones with nothing behind them.

    Reporting only what is known would let "never once checked" and "always
    right" look identical, so an unmeasured key is listed with its count of
    zero rather than left out.
    """
    outcomes = await CalibrationRepository(session).list_for()
    corpus = Calibrations.of(outcomes, scope=scope, settings=get_settings())

    records = []
    for provider, dimension, mode in _KEYS:
        record = corpus.for_(provider, dimension, mode)
        entry = DIMENSIONS[dimension]
        records.append(
            {
                "dimension": dimension,
                "label": entry.label + (f" by {mode}" if mode else ""),
                "provider": provider,
                "mode": mode,
                "unit": entry.unit,
                "checked_by": entry.checked_by,
                "checker": entry.checker,
                "checks": record.sample_count,
                "status": record.status,
                "level": record.level,
                "scope_used": record.scope_used,
                "bias": record.bias,
                "low_error": record.low_error,
                "high_error": record.high_error,
            }
        )

    return {
        "records": records,
        "never_checked": dimensions_never_checked(outcomes),
        "recent": [
            {"dimension": o.dimension, "line": evidence_line(o)} for o in outcomes[-8:]
        ],
        "total_checks": len(outcomes),
    }
