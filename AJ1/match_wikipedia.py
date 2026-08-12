"""
match_wikipedia.py — AJ1 pipeline, step 4: Wikipedia/DuckDuckGo geocoding
and fuel-type lookup for every `unclear` redispatch entry, run independent
of whether match_exact.py already resolved it (so results can be directly
compared against matches_exact.csv, not just used to fill gaps).

Ported from J1/match_web.py + J1/resolve.py + J1/config.py (read directly
during planning), adapted to AJ1's conventions:
  - AJ1's own 3-tier normalization (normalize.py) run as a quality-aware
    cascade -- raw -> light -> heavy_ae -> heavy_bare, least-cleaned first,
    escalate only on failure -- the same philosophy match_exact.py's
    registry cascade already uses, instead of J1's separate fold/raw
    variant list.
  - A real fuel-type extraction: J1 only ever used the article extract
    text for its own internal contradiction check and never persisted a
    fuel type. Here it's read from the article's own Infobox Kraftwerk
    wikitext and saved as TWO columns: `wiki_fuel` -- the actual raw text
    (e.g. "Erdgas", "Steinkohle", "Windenergie"), BRENNSTOFF preferred if
    the infobox has one, else PRIMÄRENERGIE, else (Infobox Kernkraftwerk)
    "Kernenergie" -- and `wiki_fuel_class` -- the coarse konventionell/
    erneuerbar/sonstiges bucket derived from it, kept only for internal
    gate/comparison logic (matches_exact.csv's own class granularity).
    Deliberately NOT free-text keyword matching over the whole extract,
    which was tried first and produced real false positives: "50H Chemnitz"
    matched a city-district article that happened to contain "gas" as an
    unrelated substring, and a Canaletto oil-painting article ("Ölgemälde")
    matched on "öl". Checked directly against several real plant articles:
    PRIMÄRENERGIE (Fossile Energie/Windenergie/Solarenergie/...) is present
    on the generic "Infobox Kraftwerk" template used by fossil, wind, and
    solar articles alike; BRENNSTOFF is fossil/thermal-only (present
    alongside PRIMÄRENERGIE there, per the template's own wikitext
    comment); nuclear plants use a separate "Infobox Kernkraftwerk"
    template with neither field, so the template name itself implies it.
  - DE_BBOX's lat_max widened from J1's 55.10 to 55.50 after checking
    PyPSA's own German offshore wind rows directly: Owp Sandbank
    (55.294N), Owp Dantysk (55.235N), and Butendiek (55.061N) would all be
    wrongly rejected by J1's own bound.
  - Query suffix ("Kraftwerk", "Windpark", etc.) is placed BEFORE the plant
    name, not after like J1's own literal code -- verified live against the
    real API that MediaWiki's opensearch action is word-order sensitive for
    multi-word queries, and German plant articles are conventionally titled
    "<Kraftwerkstyp> <Ort>" (e.g. "Kernkraftwerk Neckarwestheim"). J1's own
    name-then-suffix order returns zero opensearch hits in every case
    tested -- its suffix mechanism is effectively dead code; what actually
    resolves J1's real Wikipedia matches is the bare-name variant tried in
    the same call.
  - A match additionally REQUIRES a real (non-None) wiki_fuel -- per your
    rule, "otherwise it is likely not a power plant". This is exactly what's
    needed to reject the failure mode found by inspecting J1's own 22 real
    Wikipedia matches directly: most of them are just the enclosing TOWN's
    own article accepted as a location proxy (e.g. "50H Chemnitz" -> the
    city of Chemnitz, "50H Parchim" -> the town), and two are outright
    wrong (matched to "Kloster Reisach", a monastery, and "Borkum-Riffgrund
    (Naturschutzgebiet)", a nature reserve) -- none of which carry an
    Infobox Kraftwerk/Kernkraftwerk fuel field, so none of them would pass
    this gate.
  - The energy-contradiction gate (checked only once wiki_fuel_class is
    already known to be real) is deliberately lenient around "sonstiges" in
    BOTH directions: no contradiction if the entry's own class is
    "sonstiges", or if the Wikipedia-derived class is "sonstiges". Motivated
    by the systematic hydro/pumped-storage labeling mismatch documented in
    AJ1/j1_exact_match_comparison.md -- redispatch's own primaerenergieart
    inconsistently labels hydro plants Konventionell/Erneuerbar while every
    registry class bucket in this repo puts them in sonstiges; without this
    leniency the same 14-plant blind spot found there would just reappear
    here.

Gates, applied per candidate Wikipedia title, in order: not a blocked/
category title, >=5-char token overlap with the entry name, coordinates
present, inside DE_BBOX, a real fuel field found in the infobox,
energy-contradiction (as above). DuckDuckGo only runs once every cascade
tier ends "rejected" (never after a plain "no titles found" miss) --
identical rule to J1's.

Output: AJ1/temp_AJ1/matches_wikipedia.csv, one row per `unclear` entry
(matched or not), plus a printed report comparing coverage/agreement
against AJ1/temp_AJ1/matches_exact.csv. `query_used` is the literal query
string that surfaced the reported title -- a bare cleaned name, or, if a
suffix-prefixed variant ("Kraftwerk <name>") is what actually found it,
that full string, so it's visible which one did the work.

Usage: python match_wikipedia.py [limit]   (limit = process only the first
N entries, for a smoke test before a full run -- a full run is a genuinely
long network job, several hundred round-trips even with the on-disk cache
warm).
"""

import json
import os
import re
import sys
import time
import unicodedata

import pandas as pd
import requests

try:
    from ddgs import DDGS
except Exception:   # optional dependency
    DDGS = None

from paths import INPUT_DIR, TEMP_DIR
from normalize import norm_light, clean_heavy, clean_heavy_bare
from match_exact import TIGHT_CLUSTER_DEG, opsd_class, psa_class

ENTRIES    = os.path.join(TEMP_DIR, "redispatch_entries.csv")
EXACT      = os.path.join(TEMP_DIR, "matches_exact.csv")
OUT        = os.path.join(TEMP_DIR, "matches_wikipedia.csv")
CACHE_PATH = os.path.join(TEMP_DIR, "wiki_cache.json")
OPSD       = os.path.join(INPUT_DIR, "OPSD_conventional_power_plants_DE.csv")
PSA        = os.path.join(INPUT_DIR, "pypsa_powerplants_de_at_lu.csv")

HEADERS = {
    "User-Agent": "redispatch-thesis-research/AJ1 (TUM seminar; academic use)",
    "Accept": "application/json",
}
WIKI_API = "https://de.wikipedia.org/w/api.php"
# WIKI_SLEEP raised from J1's 0.4s -- a full run at 0.4s hit sustained 429s
# (dozens of consecutive throttled requests), so the base rate itself
# needed to be more conservative, not just the retry backoff.
WIKI_SLEEP = 1.0
DDG_SLEEP = 2.0
CHECKPOINT_EVERY = 25

# Widened from J1's (45.50, 55.10, 5.50, 17.20) -- see module docstring.
DE_BBOX = (45.50, 55.50, 5.50, 17.20)

TIERS = ("raw", "light", "heavy_ae", "heavy_bare")


# ---------------------------------------------------------------------------
# Title gates -- ported verbatim from J1/match_web.py
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


def title_is_blocked(title):
    if CATEGORY_PREFIX.match(title):
        return True
    cleaned = title.replace("_", " ").strip()
    if TITLE_BLOCKLIST.match(cleaned):
        return True
    if "(Band)" in title or " (Band)" in cleaned:
        return True
    return False


def _ascii_fold(s):
    s = str(s).lower()
    s = (s.replace("ß", "ss").replace("ä", "ae")
           .replace("ö", "oe").replace("ü", "ue"))
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def norm_tokens(s):
    """Lowercase ASCII tokens -- ported from J1/normalize.py; a generic
    tokenizer, not tied to J1's architecture."""
    s = _ascii_fold(s)
    s = re.sub(r"[_\-/]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_overlap_strong(entry_tokens, title):
    """At least one >=5-char token shared between the entry's own tokens
    and the Wikipedia title."""
    a = {t for t in entry_tokens if len(t) >= 5}
    b = {t for t in norm_tokens(title.replace("_", " ")).split() if len(t) >= 5}
    return bool(a & b)


def in_de_bbox(lat, lon):
    if lat is None or lon is None:
        return False
    return (DE_BBOX[0] <= float(lat) <= DE_BBOX[1]
            and DE_BBOX[2] <= float(lon) <= DE_BBOX[3])


# ---------------------------------------------------------------------------
# Fuel-type keyword classification -- ENERGY_KEYWORDS ported verbatim from
# J1/match_web.py.
ENERGY_KEYWORDS = {
    "konventionell": ["kohle", "braunkohle", "steinkohle", "gas", "erdgas",
                      "kern", "atom", "nuclear", "öl", "heizöl", "diesel",
                      "dampfkraftwerk", "gasturbine", "kombikraftwerk", "gud",
                      "thermisch", "fossil"],
    "erneuerbar":    ["wind", "solar", "photovoltaik", "wasserkraft",
                      "biomasse", "biogas", "geothermie"],
    "sonstiges":     ["pumpspeicher", "wasserkraft", "stausee", "speicherkraftwerk"],
}
_CLASS_ORDER = ("konventionell", "erneuerbar", "sonstiges")


INFOBOX_RE = re.compile(r"\{\{\s*infobox\s+([a-zA-Z]+)", re.IGNORECASE)
# [ \t]* (not \s*) between "=" and the captured value -- \s* would cross the
# newline when a field is declared but left blank (e.g. Kraftwerk Zolling's
# "| BRENNSTOFF = " is empty because per-block fuel is only in FEUERUNG) and
# greedily capture the START OF THE NEXT FIELD instead. Confirmed live: this
# bug produced "wiki_fuel" = "| FEUERUNG = Block I-IV ..." for that article.
PRIMARY_ENERGY_RE = re.compile(r"\|\s*prim[äa]renergie\s*=[ \t]*(.+)", re.IGNORECASE)
BRENNSTOFF_RE = re.compile(r"\|\s*brennstoff\s*=[ \t]*(.+)", re.IGNORECASE)

# PRIMÄRENERGIE's own vocabulary -- checked directly against real "Infobox
# Kraftwerk" articles (fossil, wind, solar all use this field; nuclear uses
# a separate infobox with neither field, handled by template name below).
PRIMARY_ENERGY_TO_CLASS = {
    "fossile energie": "konventionell",
    "kernenergie": "konventionell",
    "windenergie": "erneuerbar",
    "solarenergie": "erneuerbar",
    "sonnenenergie": "erneuerbar",
    "bioenergie": "erneuerbar",
    "biomasse": "erneuerbar",
    "geothermie": "erneuerbar",
    "wasserkraft": "sonstiges",
    "pumpspeicher": "sonstiges",
}


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _clean_wikitext_value(v):
    """[[Target]] -> Target, [[Target#Anchor|Display]] -> Display -- turns
    a raw infobox field value into plain, presentable text."""
    def repl(m):
        target, display = m.group(1), m.group(2)
        text = display if display else target.split("#")[0]
        return text.strip()
    v = _WIKILINK_RE.sub(repl, v)
    v = re.sub(r"<ref[^>]*/?>.*", "", v)  # drop a trailing <ref>...</ref>/footnote onward
    return v.strip().rstrip(",;").strip()


def extract_fuel_info(content):
    """(raw_fuel_text, coarse_class) from the article's own Infobox
    Kraftwerk/Infobox Kernkraftwerk wikitext -- deliberately NOT free-text
    keyword matching over the article extract (tried first, produced real
    false positives: see module docstring). Per your instruction: BRENNSTOFF
    preferred (e.g. "Kohle, Erdgas") if the infobox has one, else
    PRIMÄRENERGIE (e.g. "Windenergie") -- BRENNSTOFF is fossil/thermal-only,
    so it's simply absent on wind/solar articles, and PRIMÄRENERGIE is
    absent on Infobox Kernkraftwerk (nuclear), handled by template name
    below. coarse_class is only used internally (gate compatibility checks
    against the entry's own class); raw_fuel_text is the actual value
    displayed/saved."""
    if not content:
        return None, "unknown"

    m = INFOBOX_RE.search(content)
    if m and m.group(1).lower() == "kernkraftwerk":
        return "Kernenergie", "konventionell"

    m = BRENNSTOFF_RE.search(content)
    if m and _clean_wikitext_value(m.group(1)):
        # Brennstoff values are short structured lists ("Kohle, Heizöl,
        # Erdgas"), not free prose -- keyword matching is safe here, unlike
        # over the whole extract. Field declared-but-blank (e.g. a multi-
        # technology plant that only fills in FEUERUNG per block) falls
        # through to PRIMÄRENERGIE below instead of returning an empty
        # string as if it were a real value.
        raw = _clean_wikitext_value(m.group(1))
        low = raw.lower()
        cls = "unknown"
        for c in _CLASS_ORDER:
            if any(kw in low for kw in ENERGY_KEYWORDS[c]):
                cls = c
                break
        return raw, cls

    m = PRIMARY_ENERGY_RE.search(content)
    if m and _clean_wikitext_value(m.group(1)):
        raw = _clean_wikitext_value(m.group(1))
        low = raw.lower()
        for key, cls in PRIMARY_ENERGY_TO_CLASS.items():
            if key in low:
                return raw, cls
        return raw, "unknown"

    return None, "unknown"


def _safe_str(v):
    """str(v).strip(), but "" for None/NaN -- `NaN or ""` doesn't work like
    `None or ""` since a float NaN is truthy, not falsy (crashed a full run:
    some redispatch_entries.csv rows have a blank primaerenergieart, read
    back by pandas as float NaN, not an empty string)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def energy_contradicts(wiki_class, entry_class):
    """Sonstiges-lenient (your adjustment, motivated by the hydro-labeling
    mismatch in j1_exact_match_comparison.md): a class disagreement is only
    a real contradiction if BOTH sides are known and neither is
    sonstiges/unknown."""
    ec = _safe_str(entry_class).lower()
    if not ec or ec == "sonstiges" or wiki_class in ("sonstiges", "unknown"):
        return False
    return wiki_class != ec


# ---------------------------------------------------------------------------
# Suffix selection -- ported verbatim from J1/resolve.py's Stage 3/5 logic
def suffixes_for(name, entry_class):
    nl = name.lower()
    ec = _safe_str(entry_class).lower()

    if ec == "konventionell":
        suf = []
        if any(s in nl for s in ("kohle", "stk", "braunkohle", "stein")):
            suf.append("Kohlekraftwerk")
        if any(s in nl for s in ("gas", "gud", "gd", "ccgt", "ocgt", "gt")):
            suf.append("Gaskraftwerk")
        if any(s in nl for s in ("kkw", "akw", "kern", "nuclear")):
            suf.append("Kernkraftwerk")
        if any(s in nl for s in ("hkw", "bhkw", "heiz", "kwk")):
            suf.append("Heizkraftwerk")
        suf.append("Kraftwerk")
        return suf

    if ec == "sonstiges":
        return ["Pumpspeicherkraftwerk", "Wasserkraftwerk", "Speicherkraftwerk", "Kraftwerk"]

    if ec == "erneuerbar":
        is_offshore = any(s in nl for s in ("owp", "offshore"))
        if any(s in nl for s in ("owp", "windpark", "windkraft", " wp ", "_wp_", "wind")):
            return (["Offshore-Windpark", "Windpark", "Windkraftanlage"] if is_offshore
                    else ["Windpark", "Windkraftanlage"])
        if any(s in nl for s in ("solarpark", "pv", "photovolt", "solar")):
            return ["Solarpark", "Photovoltaikanlage", "PV-Freiflächenanlage"]
        return ["Windpark", "Solarpark"]

    return ["Kraftwerk"]


def _tier_key(name, tier):
    if tier == "raw":
        return str(name).strip()
    if tier == "light":
        return norm_light(name)
    if tier == "heavy_ae":
        return clean_heavy(name)
    if tier == "heavy_bare":
        return clean_heavy_bare(name)
    raise ValueError(tier)


# ---------------------------------------------------------------------------
# Wikipedia / DuckDuckGo network calls -- ported from J1/match_web.py
def _get_with_backoff(params, timeout, max_attempts=5):
    """GET against WIKI_API with 429-aware exponential backoff (honors a
    Retry-After header if present). A full 555-entry run hit sustained 429s
    with only a flat single 2s retry -- that's not a transient blip, it's
    real rate-limiting that needs an actual escalating wait. Returns the
    Response on success, or None if every attempt failed."""
    wait = 5.0
    for attempt in range(max_attempts):
        try:
            resp = requests.get(WIKI_API, params=params, headers=HEADERS, timeout=timeout)
        except Exception as e:
            if attempt == max_attempts - 1:
                print(f"    request error: {e}")
                return None
            time.sleep(wait)
            wait *= 2
            continue

        if resp.status_code == 429:
            if attempt == max_attempts - 1:
                print(f"    persistent 429 after {max_attempts} attempts, giving up")
                return None
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else wait)
            wait *= 2
            continue

        if not resp.ok:
            if attempt == max_attempts - 1:
                print(f"    non-ok status {resp.status_code}, giving up")
                return None
            time.sleep(wait)
            wait *= 2
            continue

        return resp
    return None


def wiki_opensearch(query, limit=8):
    """Returns a list of titles on success (possibly empty -- a genuine
    "no results"), or None on a request failure/error -- kept distinct from
    a genuine empty result so a failure doesn't get cached forever as
    "confirmed nothing here"."""
    resp = _get_with_backoff({
        "action": "opensearch", "search": query, "limit": limit,
        "namespace": 0, "format": "json",
    }, timeout=10)
    if resp is None:
        return None
    try:
        data = resp.json()
        return data[1] if len(data) > 1 else []
    except Exception as e:
        print(f"    opensearch parse error for {query!r}: {e}")
        return None


def wiki_query(titles):
    """Returns a dict on success (possibly empty), or None on failure --
    same not-the-same-as-empty distinction as wiki_opensearch."""
    if not titles:
        return {}
    resp = _get_with_backoff({
        "action": "query",
        "prop": "coordinates|revisions",
        "rvprop": "content", "rvslots": "main",
        "titles": "|".join(titles),
        "format": "json", "redirects": 1,
    }, timeout=12)
    if resp is None:
        return None
    try:
        pages = resp.json().get("query", {}).get("pages", {})
    except Exception as e:
        print(f"    query parse error for {titles!r}: {e}")
        return None
    out = {}
    for p in pages.values():
        title = p.get("title")
        if not title:
            continue
        coords_list = p.get("coordinates") or []
        coords = coords_list[0] if coords_list else {}
        revs = p.get("revisions") or []
        content = ""
        if revs:
            content = revs[0].get("slots", {}).get("main", {}).get("*", "") or ""
        out[title] = {
            "title": title,
            "lat": coords.get("lat"), "lon": coords.get("lon"),
            "content": content,
        }
    return out


def search_ddg(query, max_results=6):
    if DDGS is None:
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


# ---------------------------------------------------------------------------
# Cache: on-disk, keyed by literal query string / Wikipedia title -- shared
# across entries and across runs (a given query/title's result never
# depends on which entry asked for it).
def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _cached_opensearch(query, cache):
    key = f"search|{query}"
    if key in cache:
        return cache[key]
    hits = wiki_opensearch(query)
    time.sleep(WIKI_SLEEP)
    if hits is None:
        # Failure, not a genuine empty result -- don't cache it, so the
        # next attempt (this run or a later one) gets a fresh try instead
        # of being stuck with a permanently "confirmed empty" result.
        return []
    cache[key] = hits
    return hits


def _cached_page(title, cache):
    key = f"page|{title}"
    if key in cache:
        return cache[key]
    pages = wiki_query([title])
    time.sleep(WIKI_SLEEP)
    if pages is None:
        return None
    page = pages.get(title)
    cache[key] = page
    return page


# ---------------------------------------------------------------------------
def _gather_titles(key, suffixes, seen_queries, cache):
    """Returns (titles, title_query, new_query_issued) -- title_query maps
    each returned title back to the literal query string (with or without
    a suffix) that first surfaced it, so the caller can report exactly
    which variant produced a match, not just the bare tier key."""
    titles, seen_titles, title_query = [], set(), {}
    # Suffix-first ("Kraftwerk Chemnitz", not "Chemnitz Kraftwerk") -- verified
    # live against the real API: MediaWiki's opensearch action is word-order
    # sensitive for multi-word queries and only matches a title's own word
    # order, and German plant articles are conventionally titled
    # "<Kraftwerkstyp> <Ort>" (e.g. "Kernkraftwerk Neckarwestheim"), never the
    # reverse -- name-first (J1's own literal order) returns zero opensearch
    # hits in every case tested.
    variants = [f"{s} {key}" for s in suffixes] + [key]
    new_query_issued = False
    for q in variants:
        if not q or q in seen_queries:
            continue
        seen_queries.add(q)
        new_query_issued = True
        for t in _cached_opensearch(q, cache):
            if t not in seen_titles:
                seen_titles.add(t)
                titles.append(t)
                title_query[t] = q
    return titles, title_query, new_query_issued


def _evaluate_titles(titles, title_query, entry_tokens, entry_class, cache):
    """Returns a result dict: status in {match, rejected, none}.
    query_used is the literal query string (title_query[t]) that surfaced
    whichever title ends up reported, so it's visible whether a bare-name
    or a suffix-prefixed variant is what actually found it."""
    if not titles:
        return dict(status="none", reason="no_titles", title=None,
                    lat=None, lon=None, url=None, wiki_fuel=None,
                    wiki_fuel_class=None, query_used=None)

    last_reason, last_title = "no_candidates", titles[0]
    last_query = title_query.get(titles[0])
    for t in titles:
        q = title_query.get(t)
        page = _cached_page(t, cache)
        if not page:
            last_reason, last_title, last_query = "page_missing", t, q
            continue
        actual = page["title"]
        if title_is_blocked(actual):
            last_reason, last_title, last_query = "title_blocked", actual, q
            continue
        if not title_overlap_strong(entry_tokens, actual):
            last_reason, last_title, last_query = "no_token_overlap", actual, q
            continue
        lat, lon = page.get("lat"), page.get("lon")
        if lat is None or lon is None:
            last_reason, last_title, last_query = "no_coords", actual, q
            continue
        if not in_de_bbox(lat, lon):
            last_reason, last_title, last_query = "outside_de_bbox", actual, q
            continue
        wiki_fuel, wiki_class = extract_fuel_info(page.get("content", ""))
        if wiki_fuel is None:
            # No Infobox Kraftwerk/Kernkraftwerk fuel field found at all --
            # per your rule, likely not a power-plant article (this is
            # exactly what filters out J1's "found the town, not the
            # plant" false positives like plain place/monastery/nature-
            # reserve articles, none of which carry that infobox).
            last_reason, last_title, last_query = "no_fuel_type_found", actual, q
            continue
        if energy_contradicts(wiki_class, entry_class):
            last_reason, last_title, last_query = "energy_mismatch", actual, q
            continue
        return dict(status="match", title=actual, lat=float(lat), lon=float(lon),
                    url=f"https://de.wikipedia.org/wiki/{actual.replace(' ', '_')}",
                    wiki_fuel=wiki_fuel, wiki_fuel_class=wiki_class,
                    query_used=q, reason="accepted")
    return dict(status="rejected", title=last_title, lat=None, lon=None,
                url=None, wiki_fuel=None, wiki_fuel_class=None,
                query_used=last_query, reason=last_reason)


def _ddg_search_and_evaluate(name, suffixes, entry_tokens, entry_class, cache):
    if DDGS is None:
        return dict(status="none", reason="ddg_unavailable", title=None,
                    lat=None, lon=None, url=None, wiki_fuel=None,
                    wiki_fuel_class=None, query_used=None)

    last_reason, last_title, last_query = "no_ddg_hit", None, None
    for suffix in suffixes:
        query = f"{name} {suffix} wikipedia"
        key = f"ddg|{query}"
        if key in cache:
            title = cache[key]
        else:
            results = search_ddg(query)
            time.sleep(DDG_SLEEP)
            title = _ddg_wiki_title(results)
            cache[key] = title
        if not title:
            continue

        page = _cached_page(title, cache)
        if not page:
            last_reason, last_title, last_query = "page_missing", title, query
            continue
        actual = page["title"]
        if title_is_blocked(actual):
            last_reason, last_title, last_query = "title_blocked", actual, query
            continue
        if not title_overlap_strong(entry_tokens, actual):
            last_reason, last_title, last_query = "no_token_overlap", actual, query
            continue
        lat, lon = page.get("lat"), page.get("lon")
        if lat is None or lon is None:
            last_reason, last_title, last_query = "no_coords", actual, query
            continue
        if not in_de_bbox(lat, lon):
            last_reason, last_title, last_query = "outside_de_bbox", actual, query
            continue
        wiki_fuel, wiki_class = extract_fuel_info(page.get("content", ""))
        if wiki_fuel is None:
            last_reason, last_title, last_query = "no_fuel_type_found", actual, query
            continue
        if energy_contradicts(wiki_class, entry_class):
            last_reason, last_title, last_query = "energy_mismatch", actual, query
            continue
        return dict(status="match", title=actual, lat=float(lat), lon=float(lon),
                    url=f"https://de.wikipedia.org/wiki/{actual.replace(' ', '_')}",
                    wiki_fuel=wiki_fuel, wiki_fuel_class=wiki_class,
                    query_used=query, reason="accepted")
    return dict(status="none", reason=last_reason, title=last_title,
                lat=None, lon=None, url=None, wiki_fuel=None,
                wiki_fuel_class=None, query_used=last_query)


def resolve_entry(name, entry_class, cache):
    """Cascade through raw -> light -> heavy_ae -> heavy_bare (stopping at
    the first tier that resolves cleanly), then DuckDuckGo once if -- and
    only if -- the last tier tried ended 'rejected' (never after a plain
    'no titles found' miss)."""
    entry_tokens = set(clean_heavy(name).split())
    suffixes = suffixes_for(name, entry_class)
    seen_queries = set()

    last_result, last_tier = None, None
    for tier in TIERS:
        key = _tier_key(name, tier)
        if not key:
            continue
        titles, title_query, new_query_issued = _gather_titles(key, suffixes, seen_queries, cache)
        if not new_query_issued:
            # Every variant at this tier reduces to a string already tried
            # at an earlier tier (e.g. light/heavy_ae/heavy_bare all give
            # "chemnitz" for a name with nothing left to strip) -- nothing
            # new was queried, so keep the earlier tier's real result
            # instead of overwriting it with a misleading empty "none".
            continue
        res = _evaluate_titles(titles, title_query, entry_tokens, entry_class, cache)
        last_result, last_tier = res, tier
        if res["status"] == "match":
            # query_used already carries the literal variant (bare key or
            # suffix-prefixed) that actually surfaced this title -- don't
            # overwrite it with the bare tier key.
            return dict(res, source="wikipedia", tier=tier)

    if last_result is not None and last_result["status"] == "rejected":
        ddg_res = _ddg_search_and_evaluate(name, suffixes, entry_tokens, entry_class, cache)
        if ddg_res["status"] == "match":
            return dict(ddg_res, source="duckduckgo", tier="ddg")

    if last_result is None:
        last_result = dict(status="none", reason="empty_name", title=None,
                           lat=None, lon=None, url=None, wiki_fuel=None,
                           wiki_fuel_class=None, query_used=None)
    return dict(last_result, source=None, tier=last_tier)


# ---------------------------------------------------------------------------
def _load_registry_lookup():
    opsd = pd.read_csv(OPSD, encoding="utf-8-sig")
    opsd["class"] = [opsd_class(s, t) for s, t in
                     zip(opsd["energy_source"], opsd["technology"])]
    opsd_by_id = opsd.set_index("id")

    psa = pd.read_csv(PSA, low_memory=False)
    psa["class"] = [psa_class(f, t) for f, t in
                    zip(psa["Fueltype"], psa["Technology"])]
    psa_by_id = psa.set_index("id")
    return opsd_by_id, psa_by_id


def _exact_match_summary(row, opsd_by_id, psa_by_id):
    """(has_clean_hit, lat, lon, class) for an entry's exact-match result --
    centroid over whichever coordinate-bearing registry (OPSD/PSA) has a
    clean (non-conflict) hit, OPSD preferred."""
    for id_col, fuel_col, by_id, id_type in (
            ("opsd_ids", "opsd_fuel", opsd_by_id, str),
            ("psa_ids", "psa_fuel", psa_by_id, int)):
        ids, fuel = row.get(id_col), row.get(fuel_col)
        if pd.isna(ids) or pd.isna(fuel) or fuel == "conflict":
            continue
        id_list = [id_type(x.strip()) for x in str(ids).split("|")]
        rows = by_id.loc[by_id.index.isin(id_list)]
        if rows.empty:
            continue
        return True, rows["lat"].mean(), rows["lon"].mean(), rows["class"].iloc[0]
    return False, None, None, None


def _comparison_report(wiki_out):
    if not os.path.exists(EXACT):
        print("\n(matches_exact.csv not found -- skipping comparison report)")
        return

    exact_by_plant = pd.read_csv(EXACT).set_index("plant")
    opsd_by_id, psa_by_id = _load_registry_lookup()

    new_matches, diffs = [], []
    for _, wr in wiki_out[wiki_out["status"] == "match"].iterrows():
        plant = wr["plant"]
        has_clean, elat, elon, ecls = False, None, None, None
        if plant in exact_by_plant.index:
            has_clean, elat, elon, ecls = _exact_match_summary(
                exact_by_plant.loc[plant], opsd_by_id, psa_by_id)

        if not has_clean:
            new_matches.append(wr)
            continue

        dist_deg = max(abs(wr["lat"] - elat), abs(wr["lon"] - elon))
        cls_conflict = energy_contradicts(wr["wiki_fuel_class"], ecls)
        if dist_deg > TIGHT_CLUSTER_DEG or cls_conflict:
            diffs.append((plant, dist_deg, wr["wiki_fuel"], ecls, wr["title"]))

    print(f"\nnew matches (no clean exact-match hit anywhere): {len(new_matches)}")
    for wr in new_matches:
        print(f"  {wr['plant']!r} -> {wr['title']!r} ({wr['wiki_fuel']}) "
              f"[{wr['lat']:.4f},{wr['lon']:.4f}] via {wr['source']}")

    print(f"\nsignificant differences vs. exact match: {len(diffs)}")
    for plant, dist, wfuel, ecls, title in diffs:
        print(f"  {plant!r}: wiki={title!r} ({wfuel})  exact_class={ecls}  "
              f"dist~{dist:.3f} deg")


# ---------------------------------------------------------------------------
def main(entries_file: str = ENTRIES, limit: int = None) -> None:
    df = pd.read_csv(entries_file)
    unclear = df[df["category"] == "unclear"].reset_index(drop=True)
    if limit:
        unclear = unclear.head(limit)

    cache = load_cache()
    rows = []
    for i, r in unclear.iterrows():
        name, entry_class = r["plant"], r["primaerenergieart"]
        res = resolve_entry(name, entry_class, cache)
        rows.append({
            "plant": name, "status": res["status"], "source": res.get("source"),
            "tier": res.get("tier"), "query_used": res.get("query_used"),
            "title": res.get("title"), "url": res.get("url"),
            "lat": res.get("lat"), "lon": res.get("lon"),
            "wiki_fuel": res.get("wiki_fuel"),
            "wiki_fuel_class": res.get("wiki_fuel_class"),
            "reason": res.get("reason"),
        })
        if (i + 1) % CHECKPOINT_EVERY == 0:
            pd.DataFrame(rows).to_csv(OUT, index=False, encoding="utf-8")
            save_cache(cache)
            print(f"  ... {i + 1}/{len(unclear)} processed")

    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False, encoding="utf-8")
    save_cache(cache)

    print(f"\n-> {OUT}: {len(out)} rows\n")
    print(out["status"].value_counts().to_string())
    print("\nsource (of matches):")
    print(out.loc[out["status"] == "match", "source"].value_counts().to_string())
    print("\nrejection reasons:")
    print(out.loc[out["status"] == "rejected", "reason"].value_counts().to_string())

    _comparison_report(out)


if __name__ == "__main__":
    _limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(limit=_limit)
