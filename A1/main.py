"""
main.py — run the full redispatch → power-plant matching pipeline end to end.

Each stage lives in its own module with a `main()` entry point (see docs/matching-pipeline.md);
this orchestrates them in order. Stages write incrementally to this pipeline's temp_A1/ and to
results/A1/ (see paths.py), so a run can also be resumed by commenting out completed stages.

Run `python prep_data.py` first if input/Redispatch_Daten_2013_2026.csv,
input/pypsa_powerplants_de_at_lu.csv, or input/bnetza_kraftwerkliste_clean.csv
don't exist yet — this pipeline reads them read-only and doesn't build them itself.

    python A1/main.py

The candidate index (step 0) is rebuilt only if it's missing — the German PyPSA data is stable,
so delete temp_A1/candidate_index.csv to force a rebuild.
"""

import os
import time

from paths import INPUT_DIR, TEMP_DIR, RESULTS_DIR

import build_candidate_index
import redispatch_prep
import match_exact
import match_fuzzy
import match_llm
import match_clusters
import match_wikipedia
import assemble_results
import geocode_backfill
import confirm_matches

# ── config ────────────────────────────────────────────────────────────────────
# The prepped combined 2013-2026 dataset — built by prep_data.py, which must be
# run first (see its docstring / docs/matching-pipeline.md).
REDISPATCH_FILE  = os.path.join(INPUT_DIR, "Redispatch_Daten_2013_2026.csv")
INDEX_FILE       = os.path.join(TEMP_DIR, "candidate_index.csv")

# ── stages, in order ──────────────────────────────────────────────────────────
STAGES = [
    ("redispatch prep + rule filter", lambda: redispatch_prep.main(REDISPATCH_FILE)),
    ("exact match",                   match_exact.main),
    ("fuzzy shortlist",               match_fuzzy.main),
    ("LLM disambiguation",            match_llm.main),
    ("cluster matching",              match_clusters.main),
    ("Wikipedia residual",            match_wikipedia.main),
    ("assemble",                      assemble_results.main),
    ("coordinate backfill",           geocode_backfill.main),
    ("coordinate confirmation",       confirm_matches.main),
]


def banner(text: str) -> None:
    print(f"\n{'=' * 64}\n  {text}\n{'=' * 64}", flush=True)


def main() -> None:
    t0 = time.time()

    banner("step 0 · candidate index")
    if os.path.exists(INDEX_FILE):
        print(f"{INDEX_FILE} exists — skipping rebuild (delete it to force).")
    else:
        build_candidate_index.main()

    for i, (name, fn) in enumerate(STAGES, 1):
        banner(f"step {i} · {name}")
        fn()

    print(f"\n✓ pipeline complete in {time.time() - t0:.0f}s → "
          f"{os.path.join(RESULTS_DIR, 'redispatch_plant_matches.csv')}")


if __name__ == "__main__":
    main()
