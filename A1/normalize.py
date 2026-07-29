"""Shared name normalisation for the matching stages.

norm_light — the exact-match level: lowercase, strip a leading TSO prefix,
parenthesised noise, and punctuation. IDENTICAL to build_candidate_index.norm(),
so bnetza_lookup.norm_name and the index names line up with a normalised query.

(norm_heavy — the fuzzy level, with stopword/affix stripping — is added here when
the fuzzy stage is built.)
"""

import re

_TSO_PREFIX = re.compile(r"^\s*(50H|TTG|TNG|AMP|TBW)\s+", re.I)


def norm_light(name: str) -> str:
    s = str(name).strip()
    s = _TSO_PREFIX.sub("", s)
    s = s.replace("_", " ")                       # underscore is a delimiter: WP_JUECHEN → WP JUECHEN
    s = re.sub(r"\([^)]*\)", " ", s)             # drop "(KapRes)", "(ENGIE)", …
    s = re.sub(r"[^\w\säöüÄÖÜß-]", " ", s)        # punctuation → space, keep umlauts/hyphen
    return re.sub(r"\s+", " ", s).strip().lower()


# Generic plant-type words, DSO/operator prefixes, and legal suffixes that carry no
# location signal — stripped as whole tokens so the place name dominates the fuzzy score.
# Applied to BOTH query and candidate. Tunable.
_STOP = {
    # plant-type
    "kraftwerk", "kw", "hkw", "hkwm", "gud", "gtkw", "gt", "dt", "bhkw", "kwk",
    "psw", "pss", "ms", "ro", "ready", "kng", "cluster", "standort", "block",
    "windpark", "wp", "wpk", "windkraftanlage", "wka", "wind",
    "pv", "pva", "photovoltaik", "solarpark", "solar", "park", "anlage", "freiflache",
    # DSO / operator prefixes seen on cluster & query names
    "shn", "ava", "avacon", "edis", "wema", "mns", "ten", "westnetz", "lew", "vse",
    "syna", "nike", "verteilnetz",
    # legal / filler
    "gmbh", "co", "kg", "ug", "ag", "mbh", "und", "der", "die", "das",
}
_TURBINE = re.compile(r"^t\d+$")   # turbine codes T411, T412 …


def norm_heavy(name: str) -> str:
    """Fuzzy level: norm_light + strip generic type words / DSO prefixes / turbine codes."""
    toks = [t for t in norm_light(name).split()
            if t not in _STOP and not _TURBINE.match(t)]
    return " ".join(toks)
