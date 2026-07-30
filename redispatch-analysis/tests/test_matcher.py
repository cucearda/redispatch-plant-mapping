"""Regression tests for the rule-based matcher.

These pin the behaviour of the pure functions — normalisation, classification,
confidence scoring, energy-class agreement — so a refactor that changes an
output fails loudly instead of silently shifting every downstream coordinate.

    python -m pytest tests/ -q
    python tests/test_matcher.py        # also runs without pytest
"""

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "matcher"))

import classify          # noqa: E402
import confidence        # noqa: E402
import match_web         # noqa: E402
import normalize         # noqa: E402
import sources           # noqa: E402


def test_normalisation():
    assert normalize.norm("Kraftwerk Irsching-4") == "kraftwerkirsching4"
    assert normalize.ascii_fold("Kühtai Süd") == "kuehtai sued"
    assert normalize.norm_tokens("WP_JUECHEN/Nord") == "wp juechen nord"
    # cp1252/utf-8 double-encoding is repaired, not passed through
    assert normalize.fix_mojibake("KÃ¼htai") == "Kühtai"


def test_name_cleaner_strips_operator_and_block():
    cleaned, tokens, _ = normalize.clean_redispatch_name("TTG_Kraftwerk Irsching Block 4")
    assert "ttg" not in tokens
    assert "irsching" in tokens
    assert "4" not in tokens


def test_classification():
    assert classify.classify_aggregation("Börse") == "grid_area"
    assert classify.classify_aggregation("SHN Cluster Handewitt") == "cluster"
    assert classify.classify_aggregation("Kraftwerk Irsching") == "single"


def test_confidence_ordering():
    c = confidence._confidence
    # an exact registry hit must outrank every fallback
    assert c("opsd", "exact", 1.0, True, 1) == 1.0
    assert c("opsd", "exact", 1.0, True, 1) > c("psa", "exact", 1.0, True, 1)
    assert c("wiki", "match", 1.0, True, 1) > c("ddg", "match", 1.0, True, 1)
    assert c("ddg", "match", 1.0, True, 1) > c("gaz", "match", 1.0, True, 1)
    # a contradicted energy class zeroes the score outright
    assert c("opsd", "exact", 1.0, False, 1) == 0.0
    # no match means no confidence
    assert c("opsd", "none", None, True, 1) == 0.0
    # ambiguous PSA candidate sets are penalised, but never below 40%
    assert c("psa", "exact", 1.0, True, 5) < c("psa", "exact", 1.0, True, 1)
    assert c("psa", "exact", 1.0, True, 99) >= 0.4 * 0.95
    # every combination stays inside [0, 1]
    for args in itertools.product(["opsd", "psa", "wiki", "ddg", "gaz", "bogus"],
                                  ["exact", "fuzzy", "city", "match", "none"],
                                  [None, 0.0, 0.5, 1.0], [True, False, None], [0, 1, 7]):
        assert 0.0 <= c(*args) <= 1.0


def test_energy_class_gate():
    assert sources.energy_ok("konventionell", "konventionell") is True
    assert sources.energy_ok("konventionell", "erneuerbar") is False
    # an unknown class on either side must not reject — it is missing, not wrong
    assert sources.energy_ok("konventionell", None) is not False
    assert sources.energy_ok(None, "erneuerbar") is not False


def test_bounding_box_covers_de_lu_at_only():
    assert match_web.in_de_bbox(51.0, 10.0)      # Germany
    assert match_web.in_de_bbox(47.2, 11.4)      # Tirol — Austrian hydro is in scope
    assert not match_web.in_de_bbox(60.0, 10.0)  # Norway
    assert not match_web.in_de_bbox(51.0, 20.0)  # Poland


def test_blocked_wikipedia_titles():
    # Generic disambiguation-style pages: the whole title is the generic term.
    assert match_web.title_is_blocked("Kraftwerk")
    assert match_web.title_is_blocked("Windpark")
    assert match_web.title_is_blocked("Kategorie:Kraftwerk in Bayern")
    # Operator pages are blocked — an entry named after its operator must not
    # resolve to the company's article.
    assert match_web.title_is_blocked("Uniper")
    assert match_web.title_is_blocked("RWE AG")
    # A real, specific plant article is not.
    assert not match_web.title_is_blocked("Kraftwerk Irsching")
    assert not match_web.title_is_blocked("Windpark Handewitt")


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
