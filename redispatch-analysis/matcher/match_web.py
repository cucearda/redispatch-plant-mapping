"""Stages 3 and 5: Wikipedia API search, with a DuckDuckGo fallback that looks
for a Wikipedia URL and then reads that page's coordinates.

Both channels are gated: blocked titles, required token overlap with the plant
name, a Germany/AT/LU bounding box, and an energy-contradiction check.
"""

import re
import time

import requests

try:
    from ddgs import DDGS
except Exception:   # optional dependency
    DDGS = None

from config import HEADERS, WIKI_API, WIKI_SLEEP, DDG_SLEEP, DE_BBOX, USE_DDG
from normalize import ascii_fold, norm_tokens


# ---------------------------------------------------------------------------
TITLE_BLOCKLIST = re.compile(
    r"^("
    r"Kraftwerk|Blockheizkraftwerk|Virtuelles[ _]Kraftwerk|"
    r"Energiemarkt|Energiewirtschaft|"
    r"Gas[\-_ ]und[\-_ ]Dampf[\-_ ]Kombikraftwerk|"
    r"Energy[ _]Identification[ _]Code|Kraftwerk[ _]Union|"
    r"Batterie[\-_ ]Speicherkraftwerk|"
    r"TransnetBW|BASF|Infraleuna|Engadiner[ _]Kraftwerke|"
    r"Kapazit[äa]tsreserve.*|Sicherheitsbereitschaft|"
    r"EnBW|RWE[ _]?(AG)?|Uniper|Vattenfall|LEAG|STEAG|EWE|MVV|SWM|Engie|"
    r"Windpark|Solarpark"
    r")$",
    re.IGNORECASE,
)
CATEGORY_PREFIX = re.compile(r"^(Kategorie|Category):", re.IGNORECASE)

ENERGY_KEYWORDS = {
    "konventionell": ["kohle", "braunkohle", "steinkohle", "gas", "erdgas",
                      "kern", "atom", "nuclear", "öl", "heizöl", "diesel",
                      "dampfkraftwerk", "gasturbine", "kombikraftwerk", "gud",
                      "thermisch", "fossil"],
    "erneuerbar":    ["wind", "solar", "photovoltaik", "wasserkraft",
                      "biomasse", "biogas", "geothermie"],
    "sonstiges":     ["pumpspeicher", "wasserkraft", "stausee", "speicherkraftwerk"],
}


def title_is_blocked(title):
    if CATEGORY_PREFIX.match(title):
        return True
    cleaned = title.replace("_", " ").strip()
    if TITLE_BLOCKLIST.match(cleaned):
        return True
    if "(Band)" in title or " (Band)" in cleaned:
        return True
    return False


def title_overlap_strong(plant_tokens, title):
    """At least one >=5-char token shared between plant tokens and title."""
    a = {t for t in plant_tokens if len(t) >= 5}
    b = {t for t in norm_tokens(title.replace("_", " ")).split() if len(t) >= 5}
    return bool(a & b)


def in_de_bbox(lat, lon):
    if lat is None or lon is None:
        return False
    return (DE_BBOX[0] <= float(lat) <= DE_BBOX[1]
            and DE_BBOX[2] <= float(lon) <= DE_BBOX[3])


def energy_contradicts(text, primaer):
    t = (text or "").lower()
    p = (primaer or "").lower()
    if not t or p not in ENERGY_KEYWORDS:
        return False
    # For 'sonstiges' the relevant siblings are erneuerbar (hydro shares vocab);
    # only KONVENTIONELL keywords count as a real contradiction.
    if p == "sonstiges":
        own = ENERGY_KEYWORDS["sonstiges"] + ENERGY_KEYWORDS["erneuerbar"]
        opp = ENERGY_KEYWORDS["konventionell"]
    elif p == "erneuerbar":
        own = ENERGY_KEYWORDS["erneuerbar"] + ENERGY_KEYWORDS["sonstiges"]
        opp = ENERGY_KEYWORDS["konventionell"]
    else:   # konventionell
        own = ENERGY_KEYWORDS["konventionell"]
        opp = ENERGY_KEYWORDS["erneuerbar"] + ENERGY_KEYWORDS["sonstiges"]
    own_found = any(k in t for k in own)
    opp_found = any(k in t for k in opp)
    return opp_found and not own_found


def wiki_opensearch(query, limit=8):
    try:
        resp = requests.get(WIKI_API, params={
            "action": "opensearch", "search": query, "limit": limit,
            "namespace": 0, "format": "json",
        }, headers=HEADERS, timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        return data[1] if len(data) > 1 else []
    except Exception as e:
        print(f"    opensearch error: {e}")
        return []


def wiki_query(titles):
    if not titles:
        return {}
    try:
        resp = requests.get(WIKI_API, params={
            "action": "query",
            "prop": "coordinates|extracts|categories",
            "exintro": 1, "explaintext": 1, "cllimit": "max",
            "titles": "|".join(titles),
            "format": "json", "redirects": 1,
        }, headers=HEADERS, timeout=12)
        if not resp.ok:
            return {}
        pages = resp.json().get("query", {}).get("pages", {})
    except Exception as e:
        print(f"    query error: {e}")
        return {}
    out = {}
    for p in pages.values():
        title = p.get("title")
        if not title:
            continue
        coords_list = p.get("coordinates") or []
        coords = coords_list[0] if coords_list else {}
        out[title] = {
            "title": title,
            "lat": coords.get("lat"), "lon": coords.get("lon"),
            "extract": p.get("extract") or "",
        }
    return out


def search_wikipedia(plant_tokens, primaer, suffix, raw_query=None):
    """Return dict with status/title/lat/lon/url. status in
    {'match','rejected','none'}. Suffix is e.g. 'Kraftwerk', 'Windpark'.

    Tries multiple query variants (folded + umlauted, with + without suffix)
    and merges all candidate titles before applying gates. This matters
    because Wikipedia opensearch is sensitive to umlauts -- 'Görries' finds
    the Stadtteil article, 'goerries' returns unrelated people."""
    if not plant_tokens:
        return dict(status="none", title=None, lat=None, lon=None, url=None,
                    reason="empty_tokens")

    folded_query = " ".join(plant_tokens)
    variants = []
    if suffix:
        variants.append(f"{folded_query} {suffix}")
    variants.append(folded_query)
    if raw_query and raw_query.lower() != folded_query.lower():
        if suffix:
            variants.append(f"{raw_query} {suffix}")
        variants.append(raw_query)

    titles = []
    seen = set()
    for q in variants:
        for t in wiki_opensearch(q, limit=8):
            if t not in seen:
                seen.add(t); titles.append(t)
        time.sleep(WIKI_SLEEP)
    if not titles:
        return dict(status="none", title=None, lat=None, lon=None, url=None,
                    reason="no_titles")

    pages = wiki_query(titles)
    time.sleep(WIKI_SLEEP)

    last_reason = "no_candidates"
    last_title  = titles[0]
    for title in titles:
        page = pages.get(title)
        if not page:
            last_reason = "page_missing"; last_title = title; continue
        actual = page["title"]
        if title_is_blocked(actual):
            last_reason = "title_blocked"; last_title = actual; continue
        if not title_overlap_strong(plant_tokens, actual):
            last_reason = "no_token_overlap"; last_title = actual; continue
        lat, lon = page.get("lat"), page.get("lon")
        if lat is None or lon is None:
            last_reason = "no_coords"; last_title = actual; continue
        if not in_de_bbox(lat, lon):
            last_reason = "outside_de_bbox"; last_title = actual; continue
        if energy_contradicts(page.get("extract", ""), primaer):
            last_reason = "energy_mismatch"; last_title = actual; continue
        return dict(status="match", title=actual,
                    lat=float(lat), lon=float(lon),
                    url=f"https://de.wikipedia.org/wiki/{actual.replace(' ', '_')}",
                    reason="accepted")
    return dict(status="rejected", title=last_title, lat=None, lon=None,
                url=None, reason=last_reason)


# ---------------------------------------------------------------------------
def search_ddg(query, max_results=6):
    if DDGS is None or not USE_DDG:
        return []
    try:
        return list(DDGS().text(query, max_results=max_results, region="de-de"))
    except Exception as e:
        print(f"    DDG error ({type(e).__name__}): {e}")
        return []


def _ddg_wiki_title(results):
    for r in results:
        href = r.get("href", "")
        m = re.search(r"de\.wikipedia\.org/wiki/([^?#]+)", href)
        if m:
            return m.group(1).replace("_", " ")
    return None


def search_ddg_wiki(plant_tokens, primaer, suffix, raw_query=None):
    if not USE_DDG or DDGS is None or not plant_tokens:
        return dict(status="none", title=None, lat=None, lon=None, url=None,
                    reason="ddg_disabled" if not USE_DDG else "no_tokens")
    # Prefer the umlaut-bearing raw form -- DDG returns better hits for
    # 'Görries' than 'goerries'.
    base = raw_query if raw_query else " ".join(plant_tokens)
    query = f"{base} {suffix} wikipedia" if suffix else f"{base} wikipedia"
    results = search_ddg(query)
    time.sleep(DDG_SLEEP)
    title = _ddg_wiki_title(results)
    if not title:
        return dict(status="none", title=None, lat=None, lon=None, url=None,
                    reason="no_wiki_hit")
    pages = wiki_query([title])
    time.sleep(WIKI_SLEEP)
    if title not in pages:
        return dict(status="rejected", title=title, lat=None, lon=None,
                    url=None, reason="page_missing")
    page   = pages[title]
    actual = page["title"]
    if title_is_blocked(actual):
        return dict(status="rejected", title=actual, lat=None, lon=None,
                    url=None, reason="title_blocked")
    if not title_overlap_strong(plant_tokens, actual):
        return dict(status="rejected", title=actual, lat=None, lon=None,
                    url=None, reason="no_token_overlap")
    lat, lon = page.get("lat"), page.get("lon")
    if lat is None or lon is None:
        return dict(status="rejected", title=actual, lat=None, lon=None,
                    url=None, reason="no_coords")
    if not in_de_bbox(lat, lon):
        return dict(status="rejected", title=actual, lat=None, lon=None,
                    url=None, reason="outside_de_bbox")
    if energy_contradicts(page.get("extract", ""), primaer):
        return dict(status="rejected", title=actual, lat=None, lon=None,
                    url=None, reason="energy_mismatch")
    return dict(status="match", title=actual,
                lat=float(lat), lon=float(lon),
                url=f"https://de.wikipedia.org/wiki/{actual.replace(' ', '_')}",
                reason="accepted")
