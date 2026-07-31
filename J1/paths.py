"""paths.py — pipeline-local path resolution. IDENTICAL in every pipeline folder
(A1/, A2/, ...); copy verbatim into new ones, never edit per-folder. Everything
below is derived purely from this file's own location, so no path/label is ever
hardcoded — copy the folder, rename it, and it just works."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LABEL = os.path.basename(HERE)

INPUT_DIR = os.path.join(ROOT, "input")                 # shared raw data
TEMP_DIR = os.path.join(HERE, f"temp_{LABEL}")           # this pipeline's private scratch
RESULTS_DIR = os.path.join(ROOT, "results", LABEL)       # this pipeline's tracked, comparable output

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)
