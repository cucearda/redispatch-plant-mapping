"""
main.py — run the AJ1 matching pipeline end to end.

Same convention as A1/main.py: each stage is a module with its own `main()`,
orchestrated here in order. Stages write incrementally to temp_AJ1/ (see
paths.py), so a run can be resumed by commenting out completed stages.

Run `python prep_data.py` first if input/Redispatch_Daten_2013_2026.csv,
input/pypsa_powerplants_de_at_lu.csv, or input/bnetza_kraftwerkliste_clean.csv
don't exist yet — this pipeline reads them read-only and doesn't build them.

    python AJ1/main.py            # full run (stage 4 is a long network job)
    python AJ1/main.py 20         # cap stage 4 at 20 entries, for a smoke test

Note stage 1 (classify) rewrites redispatch_entries.csv in place to add the
`category` column stage 2 reads, so the two must always run as a pair — that's
why running them via this orchestrator is safer than by hand.
"""

import sys
import time

from paths import TEMP_DIR

import redispatch_prep
import classify
import match_exact
import match_wikipedia

# ponytail: cross_verify is stage 3 but is currently broken against the
# rewritten match_exact.py — it imports `_tight_cluster_pick` (gone) and reads
# opsd_id/psa_id columns that match_exact no longer writes (now pipe-joined
# {reg}_ids/{reg}_names). Re-add here once it's ported to the new schema.
# import cross_verify

STAGES = [
    ("redispatch prep",   lambda _: redispatch_prep.main()),
    ("classify",          lambda _: classify.main()),
    ("exact match",       lambda _: match_exact.main()),
    # ("cross-verify",    lambda _: cross_verify.main()),
    ("wikipedia",         lambda limit: match_wikipedia.main(limit=limit)),
]


def banner(text: str) -> None:
    print(f"\n{'=' * 64}\n  {text}\n{'=' * 64}", flush=True)


def main(wiki_limit: int = None) -> None:
    t0 = time.time()

    for i, (name, fn) in enumerate(STAGES):
        banner(f"step {i} · {name}")
        fn(wiki_limit)

    print(f"\n✓ AJ1 complete in {time.time() - t0:.0f}s → {TEMP_DIR}")
    print("  (no results/AJ1/redispatch_plant_matches.csv yet — no stage "
          "assembles one)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
