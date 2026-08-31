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

import os
import sys
import time

from paths import RESULTS_DIR

import redispatch_prep
import classify
import match_exact
import match_llm
import match_geo
import match_wikipedia
import assemble_results

# ponytail: cross_verify is stage 3 but is currently broken against the
# rewritten match_exact.py — it imports `_tight_cluster_pick` (gone) and reads
# opsd_id/psa_id columns that match_exact no longer writes (now pipe-joined
# {reg}_ids/{reg}_names). Re-add here once it's ported to the new schema.
# import cross_verify

# match_llm runs before the Wikipedia stage purely so the cheap, fast stage
# finishes first — they're independent, and assemble merges whichever ran.
#
# The geo fallback runs LAST of the matching stages, after wikipedia: it is
# the area-level fallback for entries no plant-level stage could locate, so
# it has to come after every stage that can produce a plant coordinate.
# (It used to run before wikipedia, which only wasted Nominatim calls on
# entries wikipedia went on to resolve — plant precision always outranks
# area, so the output was never wrong, just needlessly bought.)
STAGES = [
    ("redispatch prep",   lambda _: redispatch_prep.main()),
    ("classify",          lambda _: classify.main()),
    ("exact match",       lambda _: match_exact.main()),
    # ("cross-verify",    lambda _: cross_verify.main()),
    ("LLM candidate ranking", lambda _: match_llm.main()),
    ("wikipedia",         lambda limit: match_wikipedia.main(limit=limit)),
    ("geo fallback",      lambda _: match_geo.main()),
    ("assemble",          lambda _: assemble_results.main()),
]


def banner(text: str) -> None:
    print(f"\n{'=' * 64}\n  {text}\n{'=' * 64}", flush=True)


def main(wiki_limit: int = None) -> None:
    t0 = time.time()

    for i, (name, fn) in enumerate(STAGES):
        banner(f"step {i} · {name}")
        fn(wiki_limit)

    print(f"\n✓ AJ1 complete in {time.time() - t0:.0f}s → "
          f"{os.path.join(RESULTS_DIR, 'redispatch_plant_matches.csv')}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
