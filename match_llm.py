"""
match_llm.py — matching pipeline step 5 (LLM disambiguation).

Reads fuzzy_candidates.csv (top-20 per open individual entry) and asks claude-sonnet-5
to pick the correct candidate id for each, using name + fuel + Set + capacity + coords.
Structured output via messages.parse (compatible with adaptive thinking). The model may
return null when unsure — those go to the Wikipedia residual next.

Usage:
    python match_llm.py           # all open individual entries
    python match_llm.py 6         # smoke test: first 6 entries only

Output: data/matches_llm.csv (written incrementally — crash-safe).
"""

import os
import sys
import csv
from typing import Optional, Literal

import pandas as pd
import dotenv
import anthropic
from pydantic import BaseModel

dotenv.load_dotenv()

FUZZY   = "data/fuzzy_candidates.csv"
INDEX   = "data/candidate_index.csv"
ENTRIES = "data/redispatch_entries.csv"
OUT     = "data/matches_llm.csv"

MODEL       = "claude-sonnet-5"
BATCH_SIZE  = 20
MAX_TOKENS  = 16000

FIELDS = ["betroffene_anlage", "matched_id", "id_source", "method",
          "confidence", "reasoning", "matched_name", "lat", "lon"]

SYSTEM = (
    "You are an expert on German power plants and the electricity grid. Match each "
    "redispatch plant name (free text from TSO reports — operator shorthands, block "
    "numbers, abbreviated town names) to the correct candidate power plant.\n\n"
    "Rules:\n"
    "- matched_id MUST be an id copied exactly from that entry's candidate list, or null "
    "if no candidate is a plausible match.\n"
    "- Weigh fuel type, capacity, and coordinates together with the name — not the name "
    "alone. A block-level entry (e.g. 'Irsching 4') matches the plant covering that block.\n"
    "- Set: PP = generator, CHP = cogeneration (HKW/Heizkraftwerk), Storage = pumped hydro, "
    "Store = battery. Use it to disambiguate (an 'HKW' entry favours a CHP candidate).\n"
    "- confidence: high (name+attributes agree), medium (likely), low (weak/guess), "
    "none (no match → matched_id null). Keep reasoning to one sentence."
)


class PlantMatch(BaseModel):
    betroffene_anlage: str
    matched_id:        Optional[str] = None
    confidence:        Literal["high", "medium", "low", "none"]
    reasoning:         str


class PlantMatchBatch(BaseModel):
    matches: list[PlantMatch]


def build_prompt(chunk: list[tuple]) -> str:
    """chunk = [(entry_row, candidate_df)]."""
    parts = []
    for i, (e, cands) in enumerate(chunk, 1):
        lines = []
        for c in cands.itertuples():
            variants = c.match_names if isinstance(c.match_names, str) else ""
            lines.append(
                f"    id={c.cand_id}  {str(c.cand_name)[:45]}  |  variants: {variants[:70]}  |  "
                f"{c.cand_fueltype}/{c.Set}  |  {c.cand_capacity} MW  |  "
                f"{c.cand_lat},{c.cand_lon}")
        parts.append(
            f'Entry {i}: "{e.betroffene_anlage}"\n'
            f"  Energy: {e.primaerenergieart}   TSO: {e.tsos}\n"
            f"  Candidates:\n" + "\n".join(lines))
    return ("Match each entry to the correct candidate id (or null). "
            "Return one result per entry, using the exact betroffene_anlage string.\n\n"
            + "\n\n".join(parts))


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None

    fuzzy = pd.read_csv(FUZZY, low_memory=False)
    fuzzy["cand_id"] = fuzzy["cand_id"].astype(str)
    idx = pd.read_csv(INDEX, low_memory=False)
    idx["id"] = idx["id"].astype(str)
    ent = pd.read_csv(ENTRIES).set_index("betroffene_anlage")

    # attach Set + match_names (not carried in fuzzy_candidates)
    SET  = dict(zip(idx["id"], idx.get("Set", pd.Series("", index=idx.index)).fillna("")))
    MN   = dict(zip(idx["id"], idx["match_names"].fillna("")))
    NAME = dict(zip(idx["id"], idx["Name"].astype(str)))
    LAT  = dict(zip(idx["id"], idx["lat"]))
    LON  = dict(zip(idx["id"], idx["lon"]))
    fuzzy["Set"] = fuzzy["cand_id"].map(SET).fillna("")
    fuzzy["match_names"] = fuzzy["cand_id"].map(MN).fillna("")

    names = list(fuzzy["betroffene_anlage"].drop_duplicates())
    if limit:
        names = names[:limit]

    work = []
    for name in names:
        cands = fuzzy[fuzzy["betroffene_anlage"] == name].sort_values("rank")
        e = ent.loc[name]
        e = e.iloc[0] if isinstance(e, pd.DataFrame) else e
        e = type("E", (), {"betroffene_anlage": name,
                           "primaerenergieart": e["primaerenergieart"], "tsos": e["tsos"]})
        work.append((e, cands))

    client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
    chunks = [work[i:i + BATCH_SIZE] for i in range(0, len(work), BATCH_SIZE)]

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    n_match = n_null = 0
    for bn, chunk in enumerate(chunks, 1):
        print(f"  batch {bn}/{len(chunks)} ({len(chunk)} entries) …", flush=True)
        resp = client.messages.parse(
            model=MODEL, max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            messages=[{"role": "user", "content": build_prompt(chunk)}],
            output_format=PlantMatchBatch,
        )
        batch = resp.parsed_output
        if batch is None:
            print(f"    ! batch {bn} returned no parsed output (stop={resp.stop_reason})")
            continue

        valid_ids = {name: set(c["cand_id"]) for name, c in
                     ((e.betroffene_anlage, cands) for e, cands in chunk)}
        rows = []
        for m in batch.matches:
            mid = m.matched_id
            if mid is not None and mid not in valid_ids.get(m.betroffene_anlage, set()):
                mid = None                         # hallucination guard
            if mid:
                n_match += 1
                rows.append({"betroffene_anlage": m.betroffene_anlage, "matched_id": mid,
                             "id_source": "index", "method": "llm", "confidence": m.confidence,
                             "reasoning": m.reasoning, "matched_name": NAME.get(mid, ""),
                             "lat": LAT.get(mid, ""), "lon": LON.get(mid, "")})
            else:
                n_null += 1                        # → Wikipedia residual
                rows.append({"betroffene_anlage": m.betroffene_anlage, "matched_id": "",
                             "id_source": "", "method": "llm", "confidence": "none",
                             "reasoning": m.reasoning, "matched_name": "", "lat": "", "lon": ""})
        with open(OUT, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerows(rows)

    print(f"\n→ {OUT}: {n_match} matched, {n_null} null (→ residual/Wikipedia)")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    main()
