"""
geocode.py — shared Claude Haiku geocoder.

Estimates approximate German coordinates for a place / plant / region name (the old
pipeline's Stage-A approach — no external geocoding API, no new dependency). Used by
the cluster geocode channel and the final coordinate backfill.

    from geocode import geocode_names
    coords = geocode_names(["Süderdonn", "Klixbüll", ...])   # {name: (lat, lon)}
"""

import os
from typing import Optional

import anthropic
import dotenv
from pydantic import BaseModel

dotenv.load_dotenv()

MODEL = "claude-haiku-4-5"
BATCH = 40

SYSTEM = (
    "You estimate the geographic coordinates of German power-plant sites, towns, and "
    "grid regions. For each name, return its approximate latitude/longitude in Germany "
    "(WGS84), or null lat/lon if you genuinely cannot place it. Ignore operator names, "
    "block numbers, and technical abbreviations — focus on the town / site / region. "
    "Echo each name back exactly as given."
)


class Geo(BaseModel):
    name: str
    lat: Optional[float] = None
    lon: Optional[float] = None


class GeoBatch(BaseModel):
    locations: list[Geo]


def geocode_names(names, client: anthropic.Anthropic | None = None) -> dict:
    """Return {name: (lat, lon)} for the names Haiku could place."""
    client = client or anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    uniq = [n for n in dict.fromkeys(str(x) for x in names) if n.strip()]
    out: dict[str, tuple] = {}
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        prompt = "Estimate lat/lon in Germany for each name:\n" + "\n".join(f"- {n}" for n in chunk)
        resp = client.messages.parse(
            model=MODEL, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=GeoBatch)
        batch = resp.parsed_output
        if not batch:
            continue
        by_name = {g.name: g for g in batch.locations}
        for n in chunk:                       # match by echoed name, fall back to order
            g = by_name.get(n)
            if g and g.lat is not None and g.lon is not None:
                out[n] = (round(g.lat, 4), round(g.lon, 4))
    return out
