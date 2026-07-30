"""Fetch the inputs that can be fetched automatically.

    python pipeline/fetch_data.py --list     # show every source and its status
    python pipeline/fetch_data.py --opsd     # OPSD conventional power plants
    python pipeline/fetch_data.py --psa      # powerplantmatching DE/LU fleet
    python pipeline/fetch_data.py --all

The two redispatch exports are NOT downloadable without a session — they come
from the netztransparenz portal by hand. This script prints the URL and the
expected filename so the manual step is unambiguous.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
from matcher.config import DATA, REDISPATCH_SOURCES  # noqa: E402

OPSD_URL = ("https://data.open-power-system-data.org/conventional_power_plants/"
            "latest/conventional_power_plants_DE.csv")
NETZTRANSPARENZ = "https://www.netztransparenz.de/de-de/Systemdienstleistungen/Betriebsfuehrung/Redispatch"

# Derived from config so this list can never drift from what the matcher reads.
MANUAL = [
    (os.path.basename(REDISPATCH_SOURCES[1][0]),
     f"netztransparenz Redispatch archive, 2013-2020 ({REDISPATCH_SOURCES[1][1]})"),
    (os.path.basename(REDISPATCH_SOURCES[0][0]),
     f"netztransparenz Redispatch export, 2021 onwards ({REDISPATCH_SOURCES[0][1]})"),
    ("commercial_battery_storage.csv", "Marktstammdatenregister commercial battery storage extract"),
    ("grid_data/buses.csv", "PyPSA-Eur network export"),
    ("grid_data/lines.csv", "PyPSA-Eur network export"),
    ("grid_data/links.csv", "PyPSA-Eur network export"),
]


def fetch_opsd():
    import requests
    DATA.mkdir(parents=True, exist_ok=True)
    dest = DATA / "OPSD_conventional_power_plants_DE.csv"
    print(f"GET {OPSD_URL}")
    r = requests.get(OPSD_URL, timeout=120)
    r.raise_for_status()
    dest.write_bytes(r.content)
    print(f"  -> {dest}  ({len(r.content) / 1e6:.1f} MB)")


def fetch_psa():
    try:
        import powerplantmatching as pm
    except ImportError:
        sys.exit("powerplantmatching not installed:  pip install powerplantmatching")
    DATA.mkdir(parents=True, exist_ok=True)
    dest = DATA / "powerplantsPSA.csv"
    df = pm.powerplants(from_url=True)
    df = df[df["Country"].isin(["Germany", "Luxembourg"])]
    df.to_csv(dest, index=False)
    print(f"  -> {dest}  ({len(df):,} plants)")


def show_status():
    print("Automatic:")
    for name in ("OPSD_conventional_power_plants_DE.csv", "powerplantsPSA.csv"):
        print(f"  [{'x' if (DATA / name).exists() else ' '}] {name}")
    print(f"\nManual — download from {NETZTRANSPARENZ} into {DATA}/ :")
    for name, what in MANUAL:
        print(f"  [{'x' if (DATA / name).exists() else ' '}] {name:<34} {what}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--opsd", action="store_true")
    ap.add_argument("--psa", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.opsd or a.all:
        fetch_opsd()
    if a.psa or a.all:
        fetch_psa()
    if a.list or not (a.opsd or a.psa or a.all):
        show_status()
