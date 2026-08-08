"""Milestone 4 acceptance: Google's facts plus what people actually say.

    .\\.venv\\Scripts\\python.exe scripts\\discover_restaurants.py
    .\\.venv\\Scripts\\python.exe scripts\\discover_restaurants.py --query ramen --near Shinjuku

Runs the spec section 20 pipeline twice:

    1. with every source available
    2. with the Xiaohongshu tier removed

The second run is the point. Spec section 36 forbids depending on Xiaohongshu,
so the same pipeline has to keep delivering without it.

Needs GOOGLE_MAPS_API_KEY and OPENAI_API_KEY in .env.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base
from app.services.research_service import REDDIT_DOMAINS, Tier
from app.services.toolbox import MissingCredentials, Toolbox


def banner(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'-' * 76}")


def show(outcome, *, label: str) -> None:
    # Print this first: whether Xiaohongshu came back empty, failed, or was
    # never asked is the question this milestone exists to answer, and it is
    # not inferable from the absence of Xiaohongshu links below.
    print("    Research tiers:")
    if outcome.research_tiers:
        for name, result in outcome.research_tiers.items():
            print(f"       {name:<14} {result}")
    else:
        print("       (no research ran)")

    if not outcome.recommendations:
        print(f"\n    nothing recommended. warnings: {outcome.warnings}")
        return

    for index, rec in enumerate(outcome.recommendations, start=1):
        place = rec.ranked.place
        print(f"\n    {index}. {place.name}   score {rec.ranked.score.total:.3f}")

        # --- Google's half: facts.
        rating = f"{place.rating} from {place.rating_count:,}" if place.rating else "no rating"
        hours = "published" if place.opening_hours else "not published"
        print(
            f"       Google     {rating} reviews | price level {place.price_level} | hours {hours}"
        )

        # --- The community's half: taste.
        if rec.signal and rec.signal.mention_count:
            themes = "; ".join(rec.signal.themes[:3]) or "no themes extracted"
            print(
                f"       Community  {rec.signal.source_count} source(s) "
                f"({', '.join(rec.signal.source_types)}), {rec.signal.sentiment} | {themes}"
            )
            for evidence_id in rec.evidence_ids[:3]:
                record = outcome.evidence[evidence_id]
                print(f"                  [{record.source_authority}] {record.url}")
        else:
            print("       Community  nothing found for this place")

        print(f"       Dimensions {rec.ranked.score.dimensions}")

    if outcome.unresolved_mentions:
        print("\n    Mentioned but not matched to a real place:")
        for mention in outcome.unresolved_mentions[:5]:
            print(f"       {mention.name}  ({mention.resolution_note})")

    if outcome.warnings:
        print("\n    Notes:")
        for warning in outcome.warnings:
            print(f"       {warning}")

    print(f"\n    google_only: {outcome.google_only}")


async def main(query: str, near: str) -> int:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("GOOGLE_MAPS_API_KEY", settings.google_maps_api_key),
            ("OPENAI_API_KEY", settings.openai_api_key),
        )
        if not value
    ]
    if missing:
        print(f"\nMissing from .env: {', '.join(missing)}\n")
        return 1

    engine = create_async_engine(settings.database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        async with Toolbox(settings, sessions) as toolbox:
            banner(f'1. Every source available   "{query}" near {near}')
            full = await toolbox.discovery.discover(query=query, near=near, limit=4)
            show(full, label="full")

            banner("2. Xiaohongshu removed entirely")
            print("    (spec section 36: it must never be load-bearing)")
            without = await toolbox.discovery.discover(
                query=query,
                near=near,
                limit=4,
                tiers=[Tier("reddit", REDDIT_DOMAINS), Tier("open_web", None)],
            )
            show(without, label="no-xhs")

            banner("Verdict")
            print(
                f"    with everything : {len(full.recommendations)} recommendations   "
                f"(xiaohongshu: {full.research_tiers.get('xiaohongshu', 'not run')})"
            )
            print(
                f"    without XHS     : {len(without.recommendations)} recommendations   "
                f"(xiaohongshu: {without.research_tiers.get('xiaohongshu', 'not run')})"
            )
            if without.recommendations:
                print("\n    The pipeline does not depend on Xiaohongshu.")
                return 0
            print("\n    FAILED: removing Xiaohongshu emptied the recommendations.")
            return 1
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="izakaya")
    parser.add_argument("--near", default="Asakusa, Tokyo")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args.query, args.near)))
    except MissingCredentials as exc:
        sys.exit(f"\n{exc}\n")
