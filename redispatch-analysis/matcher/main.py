"""Orchestrator: run the whole rule-based matching pipeline.

    python main.py

Pools the redispatch exports into one distinct-name list, loads the OPSD and PSA
registries and the gazetteer, then resolves every name through the stage cascade
in `resolve.py`. Writes a checkpoint every 25 names, so a crashed or interrupted
run resumes cheaply from the cache.

Outputs land in `results/` (see config.py):
    plant_matcher_output.csv    one row per distinct redispatch name
    plant_matcher_output.xlsx   the same table, if openpyxl is installed
    plant_matcher_cache.json    web-lookup cache
"""

import os

import pandas as pd

from cache import load_cache, save_cache
from config import OUTPUT_PATH, unique_path
from match_geo import load_gazetteer
from resolve import resolve
from sources import load_redispatch, load_opsd, load_psa

CHECKPOINT_EVERY = 25


def main():
    print("=" * 70)
    print("Pooling redispatch sources...")
    pooled = load_redispatch()
    energy_type_map = (
        pooled.dropna(subset=["energy"])
              .groupby("plant")["energy"]
              .agg(lambda s: s.mode().iat[0] if not s.mode().empty else None)
              .to_dict()
    )
    source_map  = pooled.groupby("plant")["source"].agg(
        lambda s: "; ".join(sorted(s.unique()))).to_dict()
    event_count = pooled.groupby("plant").size().to_dict()
    plant_names = sorted(pooled["plant"].unique())
    print(f"Universal plant list: {len(plant_names)} unique plants\n")

    opsd = load_opsd()
    psa  = load_psa()
    load_gazetteer()
    load_cache()
    print()

    xlsx_path    = unique_path(OUTPUT_PATH)
    csv_path     = unique_path(xlsx_path.replace(".xlsx", ".csv"))
    partial_path = xlsx_path.replace(".xlsx", "_partial.csv")
    print(f"Output -> {os.path.basename(csv_path)}\n")

    rows = []
    for i, name in enumerate(plant_names):
        primaer = energy_type_map.get(name)
        print(f"[{i+1}/{len(plant_names)}] {name!r}  energy={primaer}")
        rec = resolve(name, primaer, opsd, psa)
        rec["n_events"] = event_count.get(name)
        rec["sources"]  = source_map.get(name)
        if rec["final_lat"] is not None:
            print(f"    -> {rec['final_source']}: "
                  f"{rec['final_lat']:.3f},{rec['final_lon']:.3f}")
        else:
            print(f"    -> {rec['final_source']}")
        rows.append(rec)
        if (i + 1) % CHECKPOINT_EVERY == 0:
            pd.DataFrame(rows).to_csv(partial_path, index=False,
                                      encoding="utf-8-sig")
            save_cache()
            print(f"    [checkpoint @ {i+1}]")

    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    save_cache()
    print(f"\nSaved CSV: {csv_path}")
    try:
        df.to_excel(xlsx_path, index=False)
        print(f"Saved Excel: {xlsx_path}")
    except ModuleNotFoundError as e:
        print(f"Excel save skipped ({e})")

    # ---- summary ----
    print("\n=== Summary ===")
    print(f"Total plants: {len(df)}")
    print(f"With coords:  {df['final_lat'].notna().sum()}")
    print("\nFinal source breakdown:")
    print(df["final_source"].value_counts())
    print("\nStage outcomes (excluding skipped/foreign):")
    work = df[~df["final_source"].isin(
        ["skipped_grid_area", "skipped_foreign"])]
    for col in ("opsd_match", "psa_match", "wiki_match", "ddg_match",
                "gaz_match", "state_match"):
        print(f"  {col:12}: {dict(work[col].value_counts())}")
    print("\nConfidence buckets:")
    bins = pd.cut(df["final_confidence"],
                  bins=[-0.001, 0.0, 0.3, 0.5, 0.7, 0.9, 1.001],
                  labels=["0 (none)", "0-0.3", "0.3-0.5", "0.5-0.7",
                          "0.7-0.9", "0.9-1.0"])
    print(bins.value_counts().sort_index())
    print("\nEnergy-type agreement on matched rows:")
    print("  OPSD energy_match:",
          dict(df.loc[df["opsd_match"] != "none", "opsd_energy_match"]
                  .value_counts(dropna=False)))
    print("  PSA  energy_match:",
          dict(df.loc[df["psa_match"] != "none", "psa_energy_match"]
                  .value_counts(dropna=False)))

    return df


if __name__ == "__main__":
    main()
