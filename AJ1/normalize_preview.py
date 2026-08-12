"""
normalize_preview.py — AJ1 pipeline: standalone inspection of the three
name-normalization tiers `match_exact.py` uses (see normalize.py), kept
separate from any matching logic so the cleaning itself can be reviewed
directly.

Input:  AJ1/temp_AJ1/redispatch_entries.csv (read-only)
Output: AJ1/temp_AJ1/redispatch_names_normalized.csv — one row per entry,
        the original `plant` string and `category` alongside its
        norm_light / clean_heavy (ae-fold) / clean_heavy_bare variants.
"""

import os

import pandas as pd

from paths import TEMP_DIR
from normalize import norm_light, clean_heavy, clean_heavy_bare

ENTRIES = os.path.join(TEMP_DIR, "redispatch_entries.csv")
OUT = os.path.join(TEMP_DIR, "redispatch_names_normalized.csv")


def main(entries_file: str = ENTRIES) -> None:
    df = pd.read_csv(entries_file)

    out = pd.DataFrame({
        "plant": df["plant"],
        "category": df["category"],
        "norm_light": df["plant"].map(norm_light),
        "clean_heavy_ae": df["plant"].map(clean_heavy),
        "clean_heavy_bare": df["plant"].map(clean_heavy_bare),
    })
    out.to_csv(OUT, index=False, encoding="utf-8")

    print(f"-> {OUT}: {len(out)} rows")


if __name__ == "__main__":
    main()
