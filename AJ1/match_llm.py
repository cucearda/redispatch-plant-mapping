"""
match_llm.py — AJ1 pipeline, step 4: rank the candidates match_exact.py
already found. Produces a "best guess", never a consolidation.

The LLM's only job is to pick one candidate out of the list that entry's
registry matches already produced, and to name a best-guess fuel where the
candidates disagree (`{reg}_fuel == "conflict"`). It is never asked to
identify a plant from scratch, so it cannot introduce a match the
registries didn't support — the deliberate design constraint from the
match_exact.py side, kept intact here.

That constraint is enforced in CODE, not just in the prompt: `_validate`
drops any `best_guess_id` that isn't in that entry's own candidate set. A
hallucinated id becomes a null guess, not a wrong match.

Only entries with something to decide are sent to the API:
  - more than one distinct candidate across the three registries, or
  - at least one registry reporting `fuel == "conflict"`.
Entries where every registry agrees on a single plant are resolved locally
with no API call (`basis="unanimous"` in the output) — they have nothing to
rank, and paying for them would be pure waste.

Output: AJ1/temp_AJ1/matches_llm.csv, written incrementally after each batch
so a crash or a rate-limit abort keeps the work already paid for. Re-running
resumes: entries already present in the file are skipped.

Usage:
    python match_llm.py           # every entry that needs ranking
    python match_llm.py 5         # smoke test: first 5 entries only
"""

import csv
import os
import sys
from typing import Literal, Optional

import anthropic
import dotenv
import pandas as pd
from pydantic import BaseModel

dotenv.load_dotenv()

from paths import TEMP_DIR

MATCHES = os.path.join(TEMP_DIR, "matches_exact.csv")
ENTRIES = os.path.join(TEMP_DIR, "redispatch_entries.csv")
OUT     = os.path.join(TEMP_DIR, "matches_llm.csv")

MODEL      = "claude-opus-5"
BATCH_SIZE = 20
MAX_TOKENS = 16000

REGISTRIES = ("opsd", "psa", "bnetza")
FIELDS = ["plant", "best_guess_id", "best_guess_registry", "best_guess_name",
          "best_guess_fuel", "llm_confidence", "llm_reasoning"]

SYSTEM = (
    "You are an expert on German power plants and the electricity grid. For each "
    "redispatch entry you are given the candidate plants that a registry name-match "
    "already found, in up to three registries (OPSD, PyPSA, BNetzA/MaStR). Pick the "
    "single candidate the redispatch entry most likely refers to.\n\n"
    "Rules:\n"
    "- best_guess_id MUST be copied exactly from that entry's own candidate list, or "
    "be null if no candidate is plausible. Never write an id that is not listed.\n"
    "- The same physical plant often appears in more than one registry. Picking any "
    "one of those rows is correct — prefer the one whose name and fuel best fit the "
    "redispatch entry.\n"
    "- best_guess_fuel matters most where the candidates disagree (shown as "
    "'conflict'): name the fuel you believe the entry's plant actually runs on. "
    "Where the candidates already agree, repeat that fuel.\n"
    "- The entry's own primaerenergieart is the operator's own label and is known to "
    "be unreliable for hydro and pumped storage — it labels those Konventionell or "
    "Erneuerbar inconsistently. Do not reject a hydro candidate on that basis alone.\n"
    "- Block/unit numbers in the entry name (e.g. 'Irsching 4') refer to a unit of the "
    "plant covering that block; a plant-level candidate is the right match for them.\n"
    "- confidence: high (name, fuel and context all agree), medium (likely), low "
    "(weak — a guess worth recording but not trusting), none (no plausible candidate "
    "→ best_guess_id null). Keep reasoning to one sentence."
)


class PlantGuess(BaseModel):
    plant:           str
    best_guess_id:   Optional[str] = None
    best_guess_fuel: Optional[str] = None
    confidence:      Literal["high", "medium", "low", "none"]
    reasoning:       str


class PlantGuessBatch(BaseModel):
    guesses: list[PlantGuess]


def _split(cell):
    """A pipe-joined match_exact.py cell -> list of strings ([] if blank)."""
    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
        return []
    return [p.strip() for p in str(cell).split(" | ") if p.strip()]


def candidates_for(row) -> list[dict]:
    """Every candidate across all three registries, flattened.

    match_exact.py writes `{reg}_ids` and `{reg}_names` as pipe-joined lists in
    the same order, so they zip positionally. `{reg}_fuel` is one value for the
    whole registry (the agreed fuel, or the literal "conflict").
    """
    out = []
    for reg in REGISTRIES:
        ids = _split(row.get(f"{reg}_ids"))
        names = _split(row.get(f"{reg}_names"))
        fuel = row.get(f"{reg}_fuel")
        fuel = "" if fuel is None or (isinstance(fuel, float) and pd.isna(fuel)) else str(fuel)
        for i, cid in enumerate(ids):
            out.append({"registry": reg, "id": cid,
                        "name": names[i] if i < len(names) else "",
                        "fuel": fuel})
    return out


def needs_ranking(cands: list[dict]) -> bool:
    """True if there is genuinely something to decide.

    One distinct plant name and no fuel conflict means the registries already
    agree — nothing for the LLM to add, so don't spend a call on it.
    """
    if len(cands) <= 1:
        return False
    if any(c["fuel"] == "conflict" for c in cands):
        return True
    return len({c["name"].strip().lower() for c in cands}) > 1


def build_prompt(chunk: list[tuple[dict, list[dict]]]) -> str:
    parts = []
    for i, (entry, cands) in enumerate(chunk, 1):
        lines = [f"    [{c['registry']}] id={c['id']}  {c['name'][:50]}  |  fuel: "
                 f"{c['fuel'] or 'unknown'}" for c in cands]
        parts.append(
            f"{i}. redispatch entry: {entry['plant']!r}\n"
            f"   declared energy type: {entry.get('primaerenergieart') or 'unknown'}"
            f"   |  TSO(s): {entry.get('tsos') or 'unknown'}"
            f"   |  max dispatched: {entry.get('max_dispatched_mw') or 'unknown'} MW"
            f"   |  active: {entry.get('sources') or 'unknown'}\n"
            "   candidates:\n" + "\n".join(lines))
    return ("Pick the best candidate for each redispatch entry below.\n\n"
            + "\n\n".join(parts))


def _validate(guess: PlantGuess, cands: list[dict]) -> Optional[dict]:
    """Map a returned id back onto a real candidate row, or None.

    This is the hard constraint: an id the model invented, or copied from a
    different entry in the same batch, has no matching candidate here and is
    dropped rather than written out as a match.
    """
    if not guess.best_guess_id:
        return None
    for c in cands:
        if c["id"] == guess.best_guess_id:
            return c
    return None


def main(limit: int = None) -> None:
    m = pd.read_csv(MATCHES)
    entries = pd.read_csv(ENTRIES).set_index("plant")

    todo, unanimous = [], 0
    for row in m.to_dict("records"):
        cands = candidates_for(row)
        if not cands:
            continue
        if not needs_ranking(cands):
            unanimous += 1
            continue
        meta = entries.loc[row["plant"]].to_dict() if row["plant"] in entries.index else {}
        todo.append(({"plant": row["plant"], **meta}, cands))

    done = set()
    if os.path.exists(OUT):
        done = set(pd.read_csv(OUT)["plant"])
        todo = [t for t in todo if t[0]["plant"] not in done]
        print(f"resuming: {len(done)} already ranked")

    if limit:
        todo = todo[:limit]

    print(f"{unanimous} entries resolved without an API call (registries unanimous)")
    print(f"{len(todo)} entries to rank via {MODEL}\n")
    if not todo:
        return

    client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    new_file = not os.path.exists(OUT)
    n_written = n_rejected = 0

    with open(OUT, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()

        for start in range(0, len(todo), BATCH_SIZE):
            chunk = todo[start:start + BATCH_SIZE]
            resp = client.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                system=SYSTEM,
                messages=[{"role": "user", "content": build_prompt(chunk)}],
                output_format=PlantGuessBatch,
            )
            by_plant = {e["plant"]: c for e, c in chunk}
            for guess in resp.parsed_output.guesses:
                cands = by_plant.get(guess.plant)
                if cands is None:
                    continue  # a plant name we didn't ask about
                hit = _validate(guess, cands)
                if guess.best_guess_id and hit is None:
                    n_rejected += 1
                writer.writerow({
                    "plant":               guess.plant,
                    "best_guess_id":       hit["id"] if hit else None,
                    "best_guess_registry": hit["registry"] if hit else None,
                    "best_guess_name":     hit["name"] if hit else None,
                    "best_guess_fuel":     guess.best_guess_fuel,
                    "llm_confidence":      "none" if hit is None else guess.confidence,
                    "llm_reasoning":       guess.reasoning,
                })
                n_written += 1
            fh.flush()
            print(f"  ... {min(start + BATCH_SIZE, len(todo))}/{len(todo)} ranked")

    print(f"\n-> {OUT}: {n_written} rows written")
    if n_rejected:
        print(f"   {n_rejected} returned ids were not in their entry's candidate "
              f"list — dropped, not matched")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
