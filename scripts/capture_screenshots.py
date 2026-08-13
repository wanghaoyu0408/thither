"""Take the documentation screenshots, from the seeded demo trip.

    python scripts/seed_demo_trip.py
    python -m uvicorn app.main:app --reload      # in another terminal
    python scripts/capture_screenshots.py

Needs playwright, which is not a project dependency because nothing but this
script uses it:

    pip install playwright && playwright install chromium

Every image comes from `trip_demo_nyc` - a fictional itinerary over real
public landmarks - so regenerating the docs never publishes anyone's actual
travel plans. Delete the trip afterwards with
`python scripts/seed_demo_trip.py --delete`.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
TRIP = "trip_demo_nyc"
OUT = ROOT / "docs" / "images"
VIEWPORT = {"width": 1440, "height": 1000}


async def settle(page, ms: int = 1200) -> None:
    await page.wait_for_timeout(ms)


async def open_panel(page, kind: str) -> None:
    """Click a chip and wait for its artifact to appear."""
    await page.click(f'[data-open="{kind}"]')
    await page.wait_for_selector(f'[data-art="{kind}"]', timeout=30_000)
    await settle(page)


async def load_all_photos(page) -> int:
    """Walk the stream so the lazy photo loader reaches every tile.

    Photos are fetched per tile as it nears the viewport, which is right for
    a real session and wrong for a screenshot of a page nobody has scrolled.
    """
    for _ in range(12):
        await page.evaluate(
            "() => { const s = document.getElementById('stream');"
            " s.scrollTop += s.clientHeight * 0.6;"
            " s.dispatchEvent(new Event('scroll')); }"
        )
        await page.wait_for_timeout(400)
    await page.evaluate("() => { document.getElementById('stream').scrollTop = 0; }")
    await settle(page)
    return await page.evaluate(
        "() => document.querySelectorAll('.thumb.loaded').length"
    )


async def shoot(page, name: str, selector: str | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    target = page.locator(selector).first if selector else page
    await target.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  docs/images/{name}.png")


async def main() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

        await page.goto(BASE, wait_until="networkidle")
        await page.evaluate(f"() => localStorage.setItem('thither.trip', '{TRIP}')")
        await page.reload(wait_until="networkidle")
        await settle(page, 2000)

        title = await page.evaluate("() => S.trip && S.trip.metadata.title")
        if title != "New York, four days":
            raise SystemExit(
                f"expected the demo trip, got {title!r}. "
                "Run: python scripts/seed_demo_trip.py"
            )

        # 1. The itinerary - the hero shot. Photographs load per tile as it
        # nears the viewport, so the whole stream is walked first.
        await open_panel(page, "itinerary")
        loaded = await load_all_photos(page)
        print(f"  {loaded} place photos loaded")
        await shoot(page, "hero")

        # 2. The stress test. It measures real routes, so give it room.
        await page.evaluate("() => { S.open.clear(); S.open.add('stress'); render(); }")
        await page.wait_for_selector(".stress-day", timeout=90_000)
        await settle(page, 1500)
        await shoot(page, "stress-test", '[data-art="stress"]')

        await browser.close()

    print("\nDone. Remove the demo trip with:")
    print("  python scripts/seed_demo_trip.py --delete")


if __name__ == "__main__":
    asyncio.run(main())
