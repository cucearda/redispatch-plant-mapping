"""
classify.py — AJ1 pipeline, step 1: label each distinct plant name from
`AJ1/temp_AJ1/redispatch_entries.csv` as a structural aggregate or not.

Combines and simplifies A1's (`A1/redispatch_prep.py::classify()`, 8-way,
upfront) and J1's (`J1/classify.py::classify_aggregation()`, 4-way, inline)
approaches into a 3-way split:

- `countertrade` — exact match on "Börse" (market countertrade; no location
  at all, not even a coarse one).
- `aggregate` — a grid/market construct with no single physical location:
  clusters, substations, regional-renewable buckets, control-reserve/network
  grid codes, demand-side/market/regulatory constructs, ENTSO-E EIC codes.
- `unclear` (the fallback) — deliberately NOT called "plant": this step only
  rules out known aggregate/non-physical constructs. Whether an entry is
  actually a real plant is only established once a later matching step
  resolves it to one (at which point it gets relabelled "plant"). This
  bucket includes, by design, several name families that look reserve/pool/
  operator-related but were checked against the real data and found to be
  genuine individual plants: the Netzreservekraftwerk/Reservekraftwerk/
  ReserveKW family (51 names, all with a bare-name twin elsewhere in the
  data), Pool/pool entries (14 names, both site-specific and
  operator-portfolio), bare TSO/DSO names (TransnetBW, EnBW Trading GmbH,
  EnBW Pumpverbot), and foreign plants (Vianden, Kühtai, Illwerke,
  Vorarlberg) — none of these are excluded from matching the way A1's
  `foreign` label does.

Output: rewrites AJ1/temp_AJ1/redispatch_entries.csv in place, adding a
`category` column.
"""

import os
import re

import pandas as pd

from paths import TEMP_DIR

ENTRIES = os.path.join(TEMP_DIR, "redispatch_entries.csv")

# Checked against all 776 distinct AJ1 names (see the plan/chat history):
# zero false positives at word-boundary granularity, and the
# Netzreservekraftwerk/Reservekraftwerk/ReserveKW family, Pool entries, bare
# TSO/DSO names, and foreign plants all correctly fall through to "unclear".
_AGGREGATE_RE = re.compile(
    r"\bCluster\b|NWAK|\bUW\b|Umspannwerk|\bEE\b|"
    r"_CR_|_TEL\b|VERTEILNETZ|UEBERTRAGUNGSNETZ|GESAMTEINSPEIS|"
    r"Abschaltbare\s*Last|Notfall|Auslaendische|Ausländische|"
    r"Netzreserve(?!kraftwerk)|Netzregelverbund|\bMRL\b|Bilanzkreis|\bBK\b|"
    r"^10Y",
    re.IGNORECASE,
)


def classify_entry(name: str) -> str:
    """Return 'countertrade' | 'aggregate' | 'unclear' for one (already
    exploded) plant name from AJ1/temp_AJ1/redispatch_entries.csv."""
    if name == "Börse":
        return "countertrade"
    if _AGGREGATE_RE.search(name):
        return "aggregate"
    return "unclear"


def main(entries_file: str = ENTRIES) -> None:
    df = pd.read_csv(entries_file)
    df["category"] = df["plant"].map(classify_entry)
    df.to_csv(entries_file, index=False, encoding="utf-8")

    print(f"-> {entries_file}: category column added")
    print(df["category"].value_counts().to_string())


if __name__ == "__main__":
    main()
