"""Refresh the vendored airport dataset.

    .\\.venv\\Scripts\\python.exe scripts\\fetch_airports.py

Source: OurAirports, public domain, no warranty of accuracy. Roughly 12.7 MB of
CSV covering every airfield the project knows about, down to grass strips.

Every row is kept - the dataset stays complete - but only the columns this
application reads are stored, and the result is gzipped. Filtering to
scheduled-service airports happens at *query* time instead, so the narrowing is
a decision the caller makes rather than one silently baked into the data.
"""

import csv
import gzip
import io
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"
TARGET = Path(__file__).resolve().parent.parent / "app" / "data" / "airports.csv.gz"

COLUMNS = [
    "ident",
    "type",
    "name",
    "latitude_deg",
    "longitude_deg",
    "iso_country",
    "municipality",
    "iata_code",
    "scheduled_service",
]


def main() -> int:
    print(f"downloading {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=120) as response:  # noqa: S310 - fixed https URL
        raw = response.read()
    print(f"  {len(raw):,} bytes")

    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    missing = [column for column in COLUMNS if column not in (reader.fieldnames or [])]
    if missing:
        print(f"source is missing expected columns: {missing}")
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with gzip.open(TARGET, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in reader:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
            rows += 1

    size = TARGET.stat().st_size
    with_iata = 0
    with gzip.open(TARGET, "rt", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["iata_code"] and row["scheduled_service"] == "yes":
                with_iata += 1

    print(f"wrote {TARGET.relative_to(TARGET.parent.parent.parent)}")
    print(f"  {rows:,} airports, {size:,} bytes gzipped")
    print(f"  {with_iata:,} with an IATA code and scheduled service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
