"""prep_data.py — shared data-preparation script (repo root).

Produces the cleaned/derived datasets that live in input/ and are read by any
pipeline (A1/, future A2/, dashboard/). Not itself a pipeline: this is dataset-
level prep with nothing pipeline-specific in it — pipelines treat input/ as an
externally-prepared, read-only layer.

Writes:
  - input/Redispatch_Daten_2013_2026.csv        the two raw redispatch exports
                                                combined, timezone-normalized,
                                                de-duplicated, with derived
                                                name/day/mwh/inc/dec columns.
  - input/pypsa_powerplants_de_at_lu.csv        input/pypsa_powerplants_europe.csv
                                                filtered to Germany + Austria +
                                                Luxembourg.
  - input/opsd_conventional_powerplants_de_at.csv  input/OPSD_conventional_power_plants_EU.csv
                                                filtered to Germany + Austria.
  - input/bnetza_kraftwerkliste_clean.csv       input/Bundesnetzagentur_Kraftwerkliste.csv
                                                cleaned/filtered/normalized.

All outputs are derived, regenerable, and gitignored (see .gitignore). Run
this before the first A1/main.py run, and again whenever a raw file under
input/ changes — nothing here is invoked automatically by A1.

Each prep_*() function is independently importable/runnable; main() runs all
of them in sequence.

Run from anywhere:  python prep_data.py
"""

import os
import re

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(HERE, "input")

BERLIN = "Europe/Berlin"

# ── prep_redispatch ──────────────────────────────────────────────────────────
REDISPATCH_RAW_FILES = [
    os.path.join(INPUT_DIR, "Redispatch_Daten_2013_2020.csv"),
    os.path.join(INPUT_DIR, "Redispatch_Daten_2021_2026.csv"),
]
REDISPATCH_COMBINED_FILE = os.path.join(INPUT_DIR, "Redispatch_Daten_2013_2026.csv")


def _num(series):
    """Parse a German comma-decimal string column into floats."""
    return pd.to_numeric(
        series.astype(str).str.replace(",", ".", regex=False),
        errors="coerce",
    )


def _local_day(df):
    """Europe/Berlin calendar day for BEGINN_DATUM+BEGINN_UHRZEIT, aware that
    ZEITZONE_VON is either 'UTC' (2013-2020 export) or 'CET'/'CEST' (2021-2026
    export, already Berlin wall-clock time)."""
    naive = pd.to_datetime(
        df["BEGINN_DATUM"] + " " + df["BEGINN_UHRZEIT"],
        format="%d.%m.%Y %H:%M", errors="coerce",
    )
    is_utc = df["ZEITZONE_VON"].astype(str).str.strip().str.upper().eq("UTC")

    day = pd.Series(pd.NA, index=df.index, dtype="object")
    utc_local = (naive[is_utc]
                 .dt.tz_localize("UTC", ambiguous="NaT", nonexistent="NaT")
                 .dt.tz_convert(BERLIN))
    day.loc[is_utc] = utc_local.dt.strftime("%Y-%m-%d")

    berlin_local = naive[~is_utc].dt.tz_localize(
        BERLIN, ambiguous="NaT", nonexistent="NaT")
    day.loc[~is_utc] = berlin_local.dt.strftime("%Y-%m-%d")
    return day


def prep_redispatch(raw_files=REDISPATCH_RAW_FILES, out_file=REDISPATCH_COMBINED_FILE):
    """Combine the raw redispatch exports into one timezone-corrected,
    de-duplicated table with derived name/day/mwh/inc/dec columns. Returns the
    combined DataFrame and writes it to out_file (sep=';', utf-8-sig, same
    dialect as the raw exports)."""
    frames = []
    for path in raw_files:
        df = pd.read_csv(path, sep=";", encoding="utf-8-sig", low_memory=False)
        df.columns = df.columns.str.strip()
        n_before = len(df)
        df = df.drop_duplicates()
        n_dupes = n_before - len(df)
        print(f"  {os.path.basename(path)}: {n_before} rows"
              + (f" ({n_dupes} exact duplicates dropped)" if n_dupes else ""))
        frames.append(df)
    r = pd.concat(frames, ignore_index=True)

    r["name"] = r["BETROFFENE_ANLAGE"].astype(str).str.strip()
    r["day"] = _local_day(r)
    r["mwh"] = _num(r["GESAMTE_ARBEIT_MWH"]).abs()
    r = r.dropna(subset=["day", "mwh"])

    # substring match, not equality: tolerant of the corrupted "erhöhen" byte
    # in a handful of 2021-2026 rows, and of either source's exact wording
    richtung = r["RICHTUNG"].astype(str)
    is_increase = richtung.str.contains("erh", case=False, na=False)
    is_decrease = richtung.str.contains("reduzieren", case=False, na=False)
    r["inc"] = r["mwh"].where(is_increase, 0.0)
    r["dec"] = r["mwh"].where(is_decrease, 0.0)

    r.to_csv(out_file, sep=";", index=False, encoding="utf-8-sig")
    print(f"  -> {os.path.basename(out_file)}: {len(r)} combined rows "
          f"(gitignored cache, regenerate freely)")
    return r


# ── prep_pypsa_countries ─────────────────────────────────────────────────────
PYPSA_EUROPE_FILE = os.path.join(INPUT_DIR, "pypsa_powerplants_europe.csv")
PYPSA_DE_AT_LU_FILE = os.path.join(INPUT_DIR, "pypsa_powerplants_de_at_lu.csv")
PYPSA_COUNTRIES = ["Germany", "Austria", "Luxembourg"]  # edit here to add/drop countries


def prep_pypsa_countries(countries=PYPSA_COUNTRIES, in_file=PYPSA_EUROPE_FILE,
                          out_file=PYPSA_DE_AT_LU_FILE):
    """Filter the full-Europe PyPSA powerplant export down to the given
    countries (default DE+AT+LU, matching the redispatch data's domestic plus
    the Austrian/Luxembourgish pumped-storage plants some redispatch entries
    reference by name)."""
    py = pd.read_csv(in_file, low_memory=False)
    n_before = len(py)
    out = py[py["Country"].isin(countries)].copy()
    out.to_csv(out_file, index=False, encoding="utf-8")
    print(f"  {os.path.basename(in_file)}: {n_before} rows -> "
          f"{os.path.basename(out_file)}: {len(out)} rows "
          f"({', '.join(countries)})")
    return out


# ── prep_opsd_countries ──────────────────────────────────────────────────────
OPSD_EU_FILE = os.path.join(INPUT_DIR, "OPSD_conventional_power_plants_EU.csv")
OPSD_DE_AT_FILE = os.path.join(INPUT_DIR, "opsd_conventional_powerplants_de_at.csv")
OPSD_COUNTRIES = ["DE", "AT"]  # edit here to add/drop countries (ISO alpha-2)


def prep_opsd_countries(countries=OPSD_COUNTRIES, in_file=OPSD_EU_FILE,
                         out_file=OPSD_DE_AT_FILE):
    """Filter the full-Europe OPSD conventional-plant export down to the given
    countries (default DE+AT, matching the redispatch data's domestic plus the
    Austrian pumped-storage plants some redispatch entries reference by name)."""
    op = pd.read_csv(in_file, low_memory=False)
    n_before = len(op)
    out = op[op["country"].isin(countries)].copy()
    out.to_csv(out_file, index=False, encoding="utf-8")
    print(f"  {os.path.basename(in_file)}: {n_before} rows -> "
          f"{os.path.basename(out_file)}: {len(out)} rows "
          f"({', '.join(countries)})")
    return out


# ── prep_bnetza_lookup ───────────────────────────────────────────────────────
BNETZA_RAW_FILE = os.path.join(INPUT_DIR, "Bundesnetzagentur_Kraftwerkliste.csv")
BNETZA_CLEAN_FILE = os.path.join(INPUT_DIR, "bnetza_kraftwerkliste_clean.csv")


def norm_bnetza_name(name):
    """Minimal name normaliser for the BNetzA lookup: strip TSO prefixes,
    parens, punctuation. NOT the same as normalize.py's norm_light/norm_heavy
    used elsewhere in A1 — kept intentionally distinct."""
    s = str(name).strip()
    s = re.sub(r"^\s*(50H|TTG|TNG|AMP|TBW)\s+", "", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^\w\säöüÄÖÜß-]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def prep_bnetza_lookup(in_file=BNETZA_RAW_FILE, out_file=BNETZA_CLEAN_FILE):
    """Clean/filter the raw BNetzA Kraftwerksliste into the stage-1 exact-match
    lookup table (mastr_id / Anzeigename / norm_name / ...)."""
    kw = pd.read_csv(in_file, sep=";", skiprows=9, encoding="latin-1", low_memory=False)
    kw.columns = [c.strip() for c in kw.columns]
    kw = kw[kw["Datensatztyp*"].isin(["Einzelanlage", "stillgelegte Anlagen"])].copy()
    kw["norm_name"] = kw["Anzeigename"].map(norm_bnetza_name)
    kw["Nettonennleistung_MW"] = pd.to_numeric(
        kw["Nettonennleistung_MW"].astype(str).str.replace(",", ".", regex=False), errors="coerce")
    out = kw.rename(columns={"EinheitMastrNummer": "mastr_id"})[
        ["mastr_id", "Anzeigename", "norm_name", "Energietraeger",
         "Nettonennleistung_MW", "Postleitzahl", "Ort"]]
    out.to_csv(out_file, index=False, encoding="utf-8")
    print(f"  {os.path.basename(out_file)}: {len(out)} rows "
          f"({out['norm_name'].nunique()} distinct normalised names)")
    return out


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print("prep 1/4 - redispatch combine + timezone/direction cleanup")
    prep_redispatch()

    print("\nprep 2/4 - PyPSA Europe -> DE+AT+LU filter")
    prep_pypsa_countries()

    print("\nprep 3/4 - OPSD conventional Europe -> DE+AT filter")
    prep_opsd_countries()

    print("\nprep 4/4 - BNetzA lookup cleanup")
    prep_bnetza_lookup()

    print("\n[OK] prep_data complete - outputs written to input/")


if __name__ == "__main__":
    main()
