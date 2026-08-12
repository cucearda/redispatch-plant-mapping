"""
normalize.py — AJ1 pipeline: the two name-normalization tiers used by the
exact-match step (`match_exact.py`).

- `norm_light` — ported verbatim from A1/normalize.py. Lowercase, strip a
  leading TSO prefix, drop parenthesised content, strip punctuation
  (keeping umlauts/hyphens), collapse whitespace. No stopword stripping —
  keeps 'exact' close to the literal name.
- `clean_heavy` — ported from J1/normalize.py's clean_redispatch_name, which
  additionally strips operator names, plant-type words, block/turbine
  codes, and generic descriptors (PREFIX_DROP/SUFFIX_DROP/DROP_ANYWHERE,
  all reused verbatim from J1). Returns just the folded string; AJ1 doesn't
  need J1's raw-token/Wikipedia-specific outputs at this stage.

Applied identically to redispatch names and every registry's own candidate
names, so a light-key or heavy-key match is a straight string comparison.
"""

import re
import unicodedata

# ---------------------------------------------------------------------------
# norm_light — verbatim from A1/normalize.py
# ---------------------------------------------------------------------------
_TSO_PREFIX_LIGHT = re.compile(r"^\s*(50H|TTG|TNG|AMP|TBW)\s+", re.I)


def norm_light(name: str) -> str:
    s = str(name).strip()
    s = _TSO_PREFIX_LIGHT.sub("", s)
    s = s.replace("_", " ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^\w\säöüÄÖÜß-]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# clean_heavy — ported from J1/normalize.py
# ---------------------------------------------------------------------------
def fix_mojibake(s):
    if not isinstance(s, str):
        return s
    try:
        repaired = s.encode("cp1252").decode("utf-8")
        return repaired if repaired != s else s
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def ascii_fold(s):
    """ä->ae, ö->oe, ü->ue, ß->ss (J1's convention)."""
    s = str(s).lower()
    s = (s.replace("ß", "ss").replace("ä", "ae")
           .replace("ö", "oe").replace("ü", "ue"))
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    return s


def ascii_fold_bare(s):
    """ä->a, ö->o, ü->u, ß->ss -- drops the umlaut mark entirely instead of
    expanding it, matching source data (e.g. PyPSA's "Janschwalde") that
    strips diacritics rather than transliterating them."""
    s = str(s).lower()
    s = s.replace("ß", "ss")
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    return s


def norm(s):
    """Squash to lowercase alphanumeric only — J1's bare exact-match key."""
    s = ascii_fold(s)
    return re.sub(r"[^a-z0-9]+", "", s)


def norm_bare(s):
    """Like norm(), but folding umlauts to the bare letter (ae -> a)."""
    s = ascii_fold_bare(s)
    return re.sub(r"[^a-z0-9]+", "", s)


PREFIX_DROP = {
    "50h", "50hertz", "amp", "amprion", "ttg", "tt", "tennet",
    "tbw", "transnetbw",
    "owp", "wp", "windpark", "windkraft", "windkraftanlage",
    "pv", "pva", "pvf", "solarpark", "solar",
    "uw", "umspannwerk", "ssw",
    "kw", "hkw", "bhkw", "hhkw", "bmkw", "gkw", "gud", "gd",
    "kkw", "psw",
    "gt", "dt", "st", "ccgt", "ocgt",
    "ee",
    "anlage", "block", "standort", "sysrel", "netzreserve", "reserve",
    "turbine", "gasturbine", "kombikraftwerk", "dampfturbine", "heizwerk",
    "maschine", "kapres", "bnbm", "gkh",
    "enbw", "rwe", "uniper", "vattenfall", "leag", "steag",
    "envia", "enviatherm", "swm", "swb", "mvv", "ewe", "engie",
    "basf", "infraleuna",
    "croc", "cluster", "shn", "ava", "bag", "nwak", "wemag",
    "cat", "crk", "sc",
    "hap",
}

COMPOUND_DROP_ENDINGS = ("kraftwerk", "speicherkraftwerk", "heizkraftwerk",
                         "wasserkraftwerk", "pumpspeicherkraftwerk",
                         "werk")

DROP_ANYWHERE = {
    "gkl", "gkm", "gkh", "gke", "gkb",
    "akw", "kkw", "dkw", "pkw", "iks", "ikw",
    "gud", "gd", "kw", "hkw", "bhkw", "bmkw",
    "psw", "kkw", "pkw",
    "croc", "cluster", "shn", "ava", "bag", "nwak", "wemag",
    "cat", "crk", "sc",
}

BLOCK_RE = re.compile(r"^bl(?:ock)?[a-z]?\d*$")
TURBINE_CODE_RE = re.compile(
    r"^[gd]t[a-z]?\d{0,3}$"
    r"|^ac\d{1,4}$"
    r"|^dc\d{1,4}$"
    r"|^psw\d+$|^kkw\d+$|^kkb\d+$"
)

SUFFIX_DROP = {
    "block", "bl", "blockc", "blocka", "blockb",
    "gud", "gd", "gt", "dt", "st",
    "neu", "alt",
    "mio", "pss", "ms", "ro",
}

TSO_PREFIX_HEAVY = re.compile(
    r"^(50H(?:ertz)?|AMP|Amprion|TTG|TT(?!G)|TenneT|TBW|TransnetBW)\b[\s_\-]*",
    re.IGNORECASE,
)


def strip_tso(name):
    return TSO_PREFIX_HEAVY.sub("", str(name)).strip()


def clean_heavy(name: str, fold=norm) -> str:
    """Aggressive cleaner (J1's clean_redispatch_name, folded-string only).

    `fold` selects the diacritic-folding convention for the token keys used
    to drive PREFIX_DROP/SUFFIX_DROP/DROP_ANYWHERE matching and to build the
    returned string -- norm (ae-expansion) by default, or norm_bare
    (bare-letter) via clean_heavy_bare below."""
    n = fix_mojibake(str(name))
    n = strip_tso(n)
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\bblock\b.*$", " ", n, flags=re.I)
    n = re.sub(r"[/_\-]+", " ", n)
    raw_toks = [t for t in re.split(r"\s+", n.strip()) if t]
    folded_toks = [fold(t) for t in raw_toks]

    while raw_toks and folded_toks[0] in PREFIX_DROP:
        raw_toks.pop(0); folded_toks.pop(0)
    while raw_toks and (folded_toks[-1] in SUFFIX_DROP
                        or folded_toks[-1].isdigit()
                        or len(folded_toks[-1]) == 1):
        raw_toks.pop(); folded_toks.pop()

    kept = []
    for r, f in zip(raw_toks, folded_toks):
        if not f or f.isdigit() or len(f) == 1:
            continue
        if f in DROP_ANYWHERE:
            continue
        if BLOCK_RE.match(f) or TURBINE_CODE_RE.match(f):
            continue
        kept.append(f)

    if len(kept) > 1:
        kept = [f for f in kept if not f.endswith(COMPOUND_DROP_ENDINGS)]

    return " ".join(kept).strip()


def clean_heavy_bare(name: str) -> str:
    """clean_heavy with bare-letter diacritic folding (ae -> a)."""
    return clean_heavy(name, fold=norm_bare)
