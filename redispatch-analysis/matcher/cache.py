"""On-disk JSON cache for the Wikipedia / DuckDuckGo / Nominatim lookups, so a
re-run costs no network traffic for names already resolved.

CACHE is mutated in place (never rebound) so `from cache import CACHE` is safe.
"""

import json
import os

from config import CACHE_PATH


CACHE = {}


def load_cache():
    """Load the JSON cache into CACHE in place."""
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                CACHE.clear()
                CACHE.update(json.load(f))
            print(f"Loaded cache: {len(CACHE)} entries")
        except Exception as e:
            print(f"  cache load failed: {e}")
            CACHE.clear()

def save_cache():
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CACHE, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        print(f"  cache save failed: {e}")
