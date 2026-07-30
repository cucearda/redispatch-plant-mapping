#!/usr/bin/env bash
# Reorganise the repository into the pipeline-stage layout.
#
# Run once, from the repo root, in Git Bash:
#     bash reorganize.sh
#
# Uses `git mv` throughout so file history survives the move. The new
# matcher/, pipeline/, maps/, tests/ and docs/ folders already exist (written
# separately) — this script only moves what git already tracks, removes the
# files the refactor supersedes, and cleans up scratch directories.
#
# It is safe to re-run: every step is guarded, and anything already in place
# is skipped.

set -u
cd "$(dirname "$0")"

moved=0; skipped=0

mv1 () {   # mv1 <src> <dest-dir>
  if [ -e "$1" ]; then
    mkdir -p "$2"
    if git ls-files --error-unmatch "$1" >/dev/null 2>&1; then
      git mv -f "$1" "$2/" && moved=$((moved+1))
    else
      mv -f "$1" "$2/" && moved=$((moved+1))
    fi
  else
    skipped=$((skipped+1))
  fi
}

mvto () {  # mvto <src> <dest-path>   (moves AND renames)
  if [ -e "$1" ]; then
    mkdir -p "$(dirname "$2")"
    if git ls-files --error-unmatch "$1" >/dev/null 2>&1; then
      git mv -f "$1" "$2" && moved=$((moved+1))
    else
      mv -f "$1" "$2" && moved=$((moved+1))
    fi
  else
    skipped=$((skipped+1))
  fi
}

echo "== 1. code packages =="
# battery_siting_score/ -> siting/ , pypsa_nodal/ -> nodal/
movedir () {   # movedir <src-dir> <dest-dir>
  [ -d "$1" ] || { skipped=$((skipped+1)); return; }
  mkdir -p "$2"
  for item in "$1"/* "$1"/.[!.]*; do
    [ -e "$item" ] || continue
    base=$(basename "$item")
    if git ls-files --error-unmatch "$item" >/dev/null 2>&1; then
      git mv -f "$item" "$2/$base" && moved=$((moved+1))
    else
      mv -f "$item" "$2/$base" && moved=$((moved+1))
    fi
  done
}

movedir battery_siting_score siting
movedir pypsa_nodal          nodal
mv1 results/compare_nodal_vs_siting.py        nodal
mv1 results/compare_nodal_vs_siting_30node.py nodal
mv1 fetch_intraday_prices_smard.py            pipeline

echo "== 2. notebooks, grouped by topic =="
mv1 redispatch_plant_match.ipynb        notebooks/matching
mv1 compare_lookup_vs_matcher.ipynb     notebooks/matching
mv1 paper_matching_figures.ipynb        notebooks/matching
mv1 redispatch_volume_analysis.ipynb    notebooks/spatial
mv1 redispatch_battery_sizing.ipynb     notebooks/sizing
mv1 siting/battery_siting_analysis.ipynb   notebooks/siting
mv1 siting/h_sweep_ptdf_bandwidth.ipynb    notebooks/siting
mv1 nodal/scigrid_vs_siting_busjoin.ipynb  notebooks/nodal
for nb in results/*.ipynb; do [ -e "$nb" ] && mv1 "$nb" notebooks/nodal; done

echo "== 3. paper fragments and decks =="
for f in *.tex; do mv1 "$f" paper; done
mv1 figures_maps/maps_figures.tex  paper
mv1 paper_data_audit.md            paper
mv1 limitations.tex                paper
mv1 Seminar_presentation_updated.pptx        paper
mv1 Battery_Siting_Scoring_Algorithm.pptx    paper

echo "== 4. data (large files are gitignored, but belong in data/) =="
mvto "Redispatch_Daten(3).csv"                        data/Redispatch_Daten_2021_2026.csv
mvto "2025-09-17 Redispatch Export 2013-2020.csv"     data/Redispatch_Daten_2013_2020.csv
mv1  intraday_prices_de_15min.csv                     data
mv1  redispatch_joined_high_confidence.csv            data
mv1  redispatch_joined.parquet                        data

echo "== 5. results =="
mv1 plant_matcher_output.xlsx            results/matcher
mv1 plant_lookup_complete_with_coords.csv results
mv1 comparison_lookup_vs_matcher.csv      results
mv1 comparison_matcher_conf_ge_0p5.csv    results
mv1 redispatch_kde_map_germany.html       results/maps

echo "== 6. figures =="
[ -d figures_maps ]  && { mkdir -p figures/maps;  git mv figures_maps/*  figures/maps/  2>/dev/null || mv figures_maps/*  figures/maps/  2>/dev/null; }
[ -d figures_paper ] && { mkdir -p figures/paper; git mv figures_paper/* figures/paper/ 2>/dev/null || mv figures_paper/* figures/paper/ 2>/dev/null; }
[ -d figures_sizing ]&& { mkdir -p figures/sizing;git mv figures_sizing/* figures/sizing/ 2>/dev/null || mv figures_sizing/* figures/sizing/ 2>/dev/null; }

echo "== 7. superseded by the refactor =="
# matcher/ replaces plant_matcher.py; maps/ replaces the three map scripts.
# Their history is preserved in the commit graph.
for f in plant_matcher.py make_redispatch_kde_map.py \
         make_redispatch_grid_kde_area_map_nosnap.py render_maps_to_png.py; do
  [ -e "$f" ] && { git rm -q --cached "$f" 2>/dev/null; rm -f "$f"; echo "  removed $f"; }
done

echo "== 8. scratch cleanup =="
rm -rf _perm_test __pycache__ */__pycache__ .ipynb_checkpoints 2>/dev/null
find . -name "__pycache__" -type d -not -path "./reference_repo/*" -not -path "./pypsa-eur/*" \
     -exec rm -rf {} + 2>/dev/null
# empty husks left behind by the moves
rmdir battery_siting_score pypsa_nodal figures_maps figures_paper figures_sizing 2>/dev/null

echo
echo "moved/removed: $moved   already-in-place: $skipped"
echo
echo "Next:"
echo "  git add -A"
echo "  git status --short          # review before committing"
echo "  python tests/test_matcher.py"
echo "  git commit -m 'Reorganise into pipeline-stage layout'"
