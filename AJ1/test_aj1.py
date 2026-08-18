"""Regression tests for the pipeline "AJ1" stages that make decisions.

Same convention as J1/test_j1.py — pure functions only, no API calls, no
files read. Covers the two things that are easy to break silently:

  1. The candidate-set constraint. match_llm.py's whole safety property is
     that the LLM can never introduce a plant the registries didn't find, and
     that rests on _validate() rejecting an unknown id. A regression there is
     invisible in the output — it just quietly produces wrong matches.

  2. The unanimous/needs-ranking agreement. match_llm.needs_ranking() decides
     what to spend an API call on; assemble_results._sole_candidate() decides
     what to resolve without one. If they disagree, entries fall in the gap:
     skipped as "already unanimous", then written out as unresolved. That bug
     shipped once already (a registry returning a whole turbine-by-turbine
     cluster counted as unanimous in one and not the other), which is why the
     agreement is pinned here rather than left to inspection.

    python -m pytest AJ1/test_aj1.py -q
    python AJ1/test_aj1.py              # also runs without pytest
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assemble_results  # noqa: E402
import match_llm         # noqa: E402
import normalize         # noqa: E402


def row(**kw):
    """A matches_exact.csv row; unspecified registries are absent."""
    r = {}
    for reg in ("opsd", "psa", "bnetza"):
        ids, names, fuel = kw.get(reg, (None, None, None))
        r[f"{reg}_ids"] = ids
        r[f"{reg}_names"] = names
        r[f"{reg}_fuel"] = fuel
    return r


# --- the candidate-set constraint ------------------------------------------
def test_validate_accepts_a_listed_id():
    cands = [{"registry": "opsd", "id": "BNA0893", "name": "GuD Schwarzheide",
              "fuel": "Natural gas"}]
    g = match_llm.PlantGuess(plant="x", best_guess_id="BNA0893",
                             confidence="high", reasoning="")
    assert match_llm._validate(g, cands) is cands[0]


def test_validate_rejects_an_unlisted_id():
    """The safety property: an id that isn't a candidate is dropped."""
    cands = [{"registry": "opsd", "id": "BNA0893", "name": "x", "fuel": "gas"}]
    g = match_llm.PlantGuess(plant="x", best_guess_id="BNA9999",
                             confidence="high", reasoning="")
    assert match_llm._validate(g, cands) is None


def test_validate_rejects_null_id():
    g = match_llm.PlantGuess(plant="x", best_guess_id=None,
                             confidence="none", reasoning="")
    assert match_llm._validate(g, []) is None


# --- candidate parsing ------------------------------------------------------
def test_candidates_zip_ids_and_names_positionally():
    cands = match_llm.candidates_for(
        row(psa=("1 | 2 | 3", "A | B | C", "Wind")))
    assert [c["id"] for c in cands] == ["1", "2", "3"]
    assert [c["name"] for c in cands] == ["A", "B", "C"]
    assert all(c["fuel"] == "Wind" for c in cands)


def test_candidates_skip_registries_with_no_hit():
    assert match_llm.candidates_for(row()) == []


# --- needs_ranking / _sole_candidate must agree -----------------------------
CASES = [
    # (label, row, expect_unanimous)
    ("single candidate",
     row(opsd=("BNA1", "Irsching", "Natural gas")), True),
    ("one site, several rows, same name",
     row(psa=("1 | 2 | 3", "Bard | Bard | Bard", "Wind")), True),
    ("two registries naming the same plant",
     row(opsd=("BNA1", "Schkopau", "Lignite"),
         psa=("7", "Schkopau", "Lignite")), True),
    ("two registries naming different plants",
     row(opsd=("BNA1", "Schkopau", "Lignite"),
         psa=("7", "Lippendorf", "Lignite")), False),
    ("fuel conflict",
     row(psa=("1 | 2", "Bedburg | Bedburg", "conflict")), False),
    ("no candidates at all",
     row(), False),
]


def test_needs_ranking_and_sole_candidate_agree():
    """Every entry is resolved by exactly one of the two paths — never both,
    never neither."""
    for label, r, expect_unanimous in CASES:
        cands = match_llm.candidates_for(r)
        ranked = match_llm.needs_ranking(cands)
        sole = assemble_results._sole_candidate(r) is not None
        assert sole == expect_unanimous, f"{label}: _sole_candidate wrong"
        if cands:
            assert ranked != sole, (
                f"{label}: needs_ranking={ranked} and unanimous={sole} — an "
                f"entry is either ranked or unanimous, never both/neither")


def test_sole_candidate_prefers_a_coordinate_bearing_registry():
    """BNetzA has no lat/lon, so it must not win over OPSD/PyPSA."""
    reg, _, _, _ = assemble_results._sole_candidate(
        row(opsd=("BNA1", "Schkopau", "Lignite"),
            bnetza=("SEE1", "Schkopau", "Braunkohle")))
    assert reg == "opsd"


# --- normalisation ----------------------------------------------------------
def test_heavy_cleaning_drops_block_numbers_light_keeps_them():
    assert normalize.clean_heavy("Ingolstadt 3") == normalize.clean_heavy("Ingolstadt")
    assert normalize.norm_light("Ingolstadt 3") != normalize.norm_light("Ingolstadt")


def test_bare_fold_matches_pypsa_style_umlaut_stripping():
    assert normalize.clean_heavy_bare("Jänschwalde") == normalize.clean_heavy_bare("Janschwalde")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
