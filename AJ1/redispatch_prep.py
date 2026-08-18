"""
redispatch_prep.py — AJ1 pipeline, step 0: reduce the redispatch export to
distinct plant-name keys.

Combines practices from A1 (`A1/redispatch_prep.py`) and J1
(`J1/sources.py::load_redispatch`), picked deliberately rather than inheriting
either wholesale:

- **Drop `Probefahrt` (test-drive) rows** before any aggregation (J1's
  practice; A1 has no equivalent filter).
- **Explode multi-plant bundles into their member names**, but using A1's
  stricter segmentation rule: a comma-joined string is only a genuine bundle
  if >=2 of its comma segments each carry a run of 4+ letters (so a single
  plant's comma-joined turbine/block-number tail, e.g. "...GT 16,17,18", is
  NOT split — J1's plain "split every comma, drop digit-only segments" rule
  would let non-digit suffix fragments like a bare "III" slip through as a
  bogus plant). Each resulting segment is then given the same treatment for
  the word " und " ("and"): a segment like "Weiher 3 und Walsum 9" is two
  real, distinct plants, but "und" glued inside a longer place name
  (Stralsund, Dortmund, Gundremmingen, Jardelund, ...) or joining a block-
  number list ("Block 6 und 8") must not be split. The rule that separates
  these correctly: only split on "und" preceded by a literal space (rules out
  every word-internal case) and followed by either a space or an uppercase
  letter (the latter catches the one observed missing-space typo,
  "...undWeiher..."), then apply the same >=2-plantish-segment gate to the
  result.
- **`max_dispatched_mw` only comes from a plant's SOLO appearances** (events
  where it was not part of an exploded bundle) — a bundle event's
  MAXIMALE_LEISTUNG_MW describes the combined dispatch, not any one member's
  own capacity, so bundle-derived rows must not inflate a member's capacity
  floor. A plant that only ever appears inside bundles gets NaN here, which
  is expected, not a bug.
- **TSO list, modal energy label, `energy_conflict`, `n_events`, and `sources`**
  all aggregate across every event mentioning the plant — solo or
  bundle-derived alike (only `max_dispatched_mw` gets the solo-only
  restriction). `sources` records whether a plant's events fall in the
  2013-2020 export, the 2021-2026 export, or both (derived from the prepped
  file's own `day` column, J1's approach).
- **Column-name cleaning and mojibake repair follow J1's more defensive
  practice** (BOM + trailing-comma strip on column names; cp1252/utf-8
  double-encoding repair on plant names) — A1 has no equivalent for either.
- **`betroffene_anlage`** carries the original, untouched raw
  `BETROFFENE_ANLAGE` string(s) (pipe-joined if more than one) each `plant`
  name was derived from — so a bundle-exploded member can still be traced
  back to exactly what appeared in the redispatch export.

Classification (individual/cluster/control_reserve/...) is deliberately NOT
done here — this step only produces the distinct-name table. Classification
is a separate, later step.

Output: temp_AJ1/redispatch_entries.csv, one row per distinct plant name.
"""

import os
import re

import pandas as pd

from paths import INPUT_DIR, TEMP_DIR

REDISPATCH = os.path.join(INPUT_DIR, "Redispatch_Daten_2013_2026.csv")
OUT = os.path.join(TEMP_DIR, "redispatch_entries.csv")


# ---------------------------------------------------------------------------
# String cleaning (J1's practice; A1 has no equivalent for either).
def _clean_columns(columns):
    return [str(c).lstrip("﻿").strip().rstrip(",").strip() for c in columns]


def fix_mojibake(s):
    """Repair cp1252/utf-8 double-encoding, e.g. 'KÃ¼htai' -> 'Kühtai'."""
    if not isinstance(s, str):
        return s
    try:
        repaired = s.encode("cp1252").decode("utf-8")
        return repaired if repaired != s else s
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


# ---------------------------------------------------------------------------
# Multi-plant bundle segmentation — A1's heuristic: only treat a split as a
# genuine bundle if >=2 of the resulting parts each carry a word of >=4
# letters. Applied first on commas, then (per resulting segment) on the
# German conjunction "und" — see the module docstring for why each rule is
# shaped the way it is.
_PLANTISH = re.compile(r"[A-Za-zÄÖÜäöüß]{4,}")

# "und" preceded by a literal space (rules out Stralsund/Dortmund/Gundremmingen/
# Jardelund/Schafflund/Riffgrund, all "...und" glued onto a longer word) and
# followed by either a space or an uppercase letter (the latter catches the
# one observed missing-space typo, "...9 undWeiher...").
_UND_SPLIT = re.compile(r"(?<= )und(?=\s|[A-ZÄÖÜ])")


def _split_if_plantish(parts: list[str]) -> list[str] | None:
    """`parts` if >=2 of them carry a 4+ letter word, else None (not a
    genuine split — caller should keep the original, unsplit string)."""
    return parts if sum(bool(_PLANTISH.search(p)) for p in parts) >= 2 else None


def bundle_segments(name: str) -> list[str]:
    """The member names of a genuine multi-plant bundle ([] if `name` isn't
    one). Splits on commas first, then re-checks each resulting segment for
    an internal "und" split (e.g. the comma segment "Weiher 3 und Walsum 9"
    becomes two members)."""
    top = _split_if_plantish([p.strip() for p in name.split(",")]) or [name]

    segments = []
    for seg in top:
        sub = _split_if_plantish([p.strip() for p in _UND_SPLIT.split(seg)])
        segments.extend(sub if sub else [seg])

    return segments if len(segments) >= 2 else []


# ---------------------------------------------------------------------------
def main(redispatch_file: str = REDISPATCH) -> None:
    df = pd.read_csv(redispatch_file, sep=";", encoding="utf-8-sig", low_memory=False)
    df.columns = _clean_columns(df.columns)
    n_raw = len(df)

    df = df[df["GRUND_DER_MASSNAHME"] != "Probefahrt"]
    n_dropped_probefahrt = n_raw - len(df)

    # notna() first: pandas >=3.0's astype(str) PRESERVES NaN rather than
    # rendering it as the literal "nan" string 2.x produced, so the "nan"
    # guard below can't be relied on to drop null plant names on its own.
    df = df[df["BETROFFENE_ANLAGE"].notna()]
    df["BETROFFENE_ANLAGE"] = df["BETROFFENE_ANLAGE"].astype(str).str.strip()
    df = df[df["BETROFFENE_ANLAGE"].ne("") & df["BETROFFENE_ANLAGE"].ne("nan")]
    df["MAXIMALE_LEISTUNG_MW"] = pd.to_numeric(
        df["MAXIMALE_LEISTUNG_MW"].astype(str).str.replace(",", ".", regex=False),
        errors="coerce")

    # source era, per event — from the prepped file's own Europe/Berlin-corrected
    # 'day' column (J1's approach), not re-derived from a raw timestamp
    df["source_tag"] = df["day"].map(
        lambda d: "2013-2020" if str(d) < "2021-01-01" else "2021-2026")

    # ---- explode: one row per (event, member plant name) --------------------
    # raw_name keeps the untouched BETROFFENE_ANLAGE string (pre-explode,
    # pre-mojibake-fix) so each output row can be traced back to exactly what
    # appeared in the redispatch export.
    rows = []
    n_bundle_events = 0
    for r in df.itertuples(index=False):
        segs = bundle_segments(r.BETROFFENE_ANLAGE)
        if segs:
            n_bundle_events += 1
            for seg in segs:
                name = fix_mojibake(seg)
                if name:
                    rows.append((name, r.PRIMAERENERGIEART, r.MAXIMALE_LEISTUNG_MW,
                                 r.ANWEISENDER_UENB, r.source_tag, True,
                                 r.BETROFFENE_ANLAGE))
        else:
            name = fix_mojibake(r.BETROFFENE_ANLAGE)
            rows.append((name, r.PRIMAERENERGIEART, r.MAXIMALE_LEISTUNG_MW,
                         r.ANWEISENDER_UENB, r.source_tag, False,
                         r.BETROFFENE_ANLAGE))

    exploded = pd.DataFrame(rows, columns=[
        "name", "primaerenergieart", "max_mw", "tso", "source_tag",
        "is_bundle_segment", "raw_name"])

    # ---- aggregate per distinct plant name ------------------------------------
    out_rows = []
    for name, g in exploded.groupby("name", sort=True):
        energies = g["primaerenergieart"].dropna().astype(str).str.strip()
        energies = energies[energies.ne("")]
        modal = energies.mode().iloc[0] if len(energies) else ""
        tsos = sorted(g["tso"].dropna().astype(str).str.strip().unique())
        sources = sorted(g["source_tag"].dropna().unique())
        raw_names = sorted(g["raw_name"].dropna().unique())

        solo_mw = g.loc[~g["is_bundle_segment"], "max_mw"]
        max_dispatched_mw = solo_mw.max() if solo_mw.notna().any() else float("nan")

        out_rows.append({
            "plant":               name,
            "betroffene_anlage":   " | ".join(raw_names),
            "primaerenergieart":   modal,
            "energy_conflict":     energies.nunique() > 1,
            "tsos":                ",".join(tsos),
            "sources":             ",".join(sources),
            "n_events":            len(g),
            "max_dispatched_mw":   max_dispatched_mw,
        })

    out = pd.DataFrame(out_rows)
    out.to_csv(OUT, index=False, encoding="utf-8")

    # ---- report ----------------------------------------------------------------
    bundle_only = exploded.groupby("name")["is_bundle_segment"].all()
    n_bundle_only = int(bundle_only.sum())
    n_nan_capacity = int(out["max_dispatched_mw"].isna().sum())
    n_conflict = int(out["energy_conflict"].sum())

    print(f"raw rows: {n_raw}  (dropped {n_dropped_probefahrt} Probefahrt rows)")
    print(f"events exploded as a genuine multi-plant bundle: {n_bundle_events}")
    print(f"\n-> {OUT}: {len(out)} distinct plant names")
    print(f"  bundle-only names (never appear solo):            {n_bundle_only}")
    print(f"  names with no max_dispatched_mw (bundle-only, expected NaN): {n_nan_capacity}")
    print(f"  energy-type conflicts flagged:                    {n_conflict}")

    # self-check
    assert out["plant"].is_unique, "duplicate keys"
    assert out["max_dispatched_mw"].notna().any(), "no dispatched-power values parsed"
    print("self-check OK")


if __name__ == "__main__":
    main()
