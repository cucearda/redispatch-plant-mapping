"""Name normalisation and the redispatch name cleaner.

`norm` / `norm_tokens` produce the match keys; `clean_redispatch_name` strips
TSO codes, operator labels, plant-type words and block/turbine suffixes down to
the place name that actually carries location signal.
"""

import re
import unicodedata


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
    s = str(s).lower()
    s = (s.replace("ß", "ss").replace("ä", "ae")
           .replace("ö", "oe").replace("ü", "ue"))
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    return s


def norm(s):
    """Squash to lowercase alphanumeric only — for exact-match keys."""
    s = ascii_fold(s)
    return re.sub(r"[^a-z0-9]+", "", s)


def norm_tokens(s):
    """Lowercase ASCII tokens separated by single spaces."""
    s = ascii_fold(s)
    s = re.sub(r"[_\-/]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Prefixes that appear at the START of a token list and should be dropped.
# Captures TSO codes, operator labels, plant-type abbreviations.
PREFIX_DROP = {
    # TSO codes
    "50h", "50hertz", "amp", "amprion", "ttg", "tt", "tennet",
    "tbw", "transnetbw",
    # plant-type abbreviations
    "owp", "wp", "windpark", "windkraft", "windkraftanlage",
    "pv", "pva", "pvf", "solarpark", "solar",
    "uw", "umspannwerk", "ssw",
    "kw", "hkw", "bhkw", "hhkw", "bmkw", "gkw", "gud", "gd",   # GD = Gas-Dampf
    "kkw", "psw",
    "gt", "dt", "st", "ccgt", "ocgt",
    # generic erneuerbar prefix
    "ee",
    # generic site descriptors
    "anlage", "block", "standort", "sysrel", "netzreserve", "reserve",
    "turbine", "gasturbine", "kombikraftwerk", "dampfturbine", "heizwerk",
    "maschine", "kapres", "bnbm", "gkh",
    # operator names that show up as prefix tokens
    "enbw", "rwe", "uniper", "vattenfall", "leag", "steag",
    "envia", "enviatherm", "swm", "swb", "mvv", "ewe", "engie",
    "basf", "infraleuna",
    # cluster / TSO sub-area markers — ported from v2's CLUSTER_MARKERS.
    # Directional tokens (nord/sued/ost/west) are intentionally NOT here
    # because the state matcher needs them as direction modifiers.
    "croc", "cluster", "shn", "ava", "bag", "nwak", "wemag",
    "cat", "crk", "sc",
    # unit/operator prefixes seen in the data
    "hap",   # 'HAP' on Halle entries — Halle Anlage Power or similar
}

# Long compound tokens that should also be stripped wherever they appear in
# the cleaned name, unless they are the ONLY surviving token (which would
# mean the whole name is just "Kraftwerk"). 'werk' is included so compounds
# like 'gersteinwerk' clean to 'gerstein'.
COMPOUND_DROP_ENDINGS = ("kraftwerk", "speicherkraftwerk", "heizkraftwerk",
                         "wasserkraftwerk", "pumpspeicherkraftwerk",
                         "werk")

# Short asset / plant-type abbreviations that show up MID-string (not just at
# the head). Applied to every token. Catches 'GKL' in 'Linden GKL DT',
# 'NWAK' in 'BAG NWAK-Cluster ...' (when not at head), etc.
DROP_ANYWHERE = {
    # Großkraftwerk / Heizkraftwerk / Atom- / Diesel- abbreviations
    "gkl", "gkm", "gkh", "gke", "gkb",
    "akw", "kkw", "dkw", "pkw", "iks", "ikw",
    # plant-type abbreviations also useful mid-string
    "gud", "gd", "kw", "hkw", "bhkw", "bmkw",
    "psw", "kkw", "pkw",
    # cluster / TSO sub-area markers (also in PREFIX_DROP, repeated for
    # safety so they're killed even if a mid-string occurrence sneaks in)
    "croc", "cluster", "shn", "ava", "bag", "nwak", "wemag",
    "cat", "crk", "sc",
}
# Block markers like 'block', 'blockA', 'blockB1' — handled by regex below.
BLOCK_RE = re.compile(r"^bl(?:ock)?[a-z]?\d*$")
# Turbine / converter unit codes: gt, dt, gtb1, dtc0, gtf2, ac101, dc202, ...
# Drop wherever they appear. Matches:
#   - [gd]t followed by optional letter and digits  (gas/dampf turbine block)
#   - ac / dc converter station codes
#   - psw / kkw block codes ending in digits
TURBINE_CODE_RE = re.compile(
    r"^[gd]t[a-z]?\d{0,3}$"
    r"|^ac\d{1,4}$"
    r"|^dc\d{1,4}$"
    r"|^psw\d+$|^kkw\d+$|^kkb\d+$"
)

# Suffix tokens we trim from the end (block letters, technology codes).
SUFFIX_DROP = {
    "block", "bl", "blockc", "blocka", "blockb",
    "gud", "gd", "gt", "dt", "st",
    # NOTE: nord/sued/ost/west kept IN the token list so the state matcher
    # can read them as directional modifiers (Bayern_Nord -> north half).
    "neu", "alt",
    "mio", "pss", "ms", "ro",
}


def significant_tokens(name):
    """Tokens used by the aggregation check (drops descriptor noise)."""
    out = []
    for t in norm_tokens(strip_tso(name)).split():
        if t in PREFIX_DROP or t.endswith("kraftwerk") or t.isdigit():
            continue
        out.append(t)
    return out


TSO_PREFIX = re.compile(
    r"^(50H(?:ertz)?|AMP|Amprion|TTG|TT(?!G)|TenneT|TBW|TransnetBW)\b[\s_\-]*",
    re.IGNORECASE,
)

def strip_tso(name):
    return TSO_PREFIX.sub("", str(name)).strip()


def clean_redispatch_name(name):
    """
    Aggressive cleaner. Returns (cleaned_folded, folded_tokens, cleaned_raw).

      cleaned_folded  - ASCII-folded space-joined string (used for exact keys)
      folded_tokens   - list, used by all matchers and gates
      cleaned_raw     - original case + umlauts preserved; used for Wikipedia
                        opensearch where 'Görries' beats 'goerries'.
    """
    n = fix_mojibake(str(name))
    n = strip_tso(n)
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\bblock\b.*$", " ", n, flags=re.I)
    # Hyphens and slashes become WHITESPACE (not removed). Crucial: keeps
    # 'Halle-Dieselstr' and 'Dörpen-West' tokenizable so each half can be
    # searched independently.
    n = re.sub(r"[/_\-]+", " ", n)
    raw_toks = [t for t in re.split(r"\s+", n.strip()) if t]
    folded_toks = [norm(t) for t in raw_toks]

    # head trim against PREFIX_DROP
    while raw_toks and folded_toks[0] in PREFIX_DROP:
        raw_toks.pop(0); folded_toks.pop(0)
    # tail trim against SUFFIX_DROP / digits / single letters
    while raw_toks and (folded_toks[-1] in SUFFIX_DROP
                        or folded_toks[-1].isdigit()
                        or len(folded_toks[-1]) == 1):
        raw_toks.pop(); folded_toks.pop()

    # Now apply ANYWHERE filters: drop short asset codes, residual digits,
    # block-letter markers, turbine/converter unit codes wherever they sit.
    kept = []
    for r, f in zip(raw_toks, folded_toks):
        if not f or f.isdigit() or len(f) == 1:
            continue
        if f in DROP_ANYWHERE:
            continue
        if BLOCK_RE.match(f) or TURBINE_CODE_RE.match(f):
            continue
        kept.append((r, f))

    # drop long compound 'kraftwerk' tokens unless they're the only survivor
    if len(kept) > 1:
        kept = [(r, f) for r, f in kept
                if not f.endswith(COMPOUND_DROP_ENDINGS)]
    raw_toks    = [r for r, _ in kept]
    folded_toks = [f for _, f in kept]
    return (" ".join(folded_toks).strip(),
            folded_toks,
            " ".join(raw_toks).strip())
