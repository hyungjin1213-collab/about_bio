from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUTPUT = ROOT / "output"


def _manual_professors() -> pd.DataFrame:
    seed = pd.read_csv(DATA / "professors_seed.csv", dtype=str, encoding="utf-8-sig").fillna("")
    return seed[seed["identity_status"] == "manual"]


def test_every_hand_entered_professor_is_on_the_map():
    # Hand-entered professors (identity_status = manual) must reach the map data and the
    # enriched list; a collection run must never silently drop them (10/31 report).
    manual = _manual_professors()
    nodes = pd.read_csv(OUTPUT / "network_nodes.csv", dtype=str, encoding="utf-8-sig").fillna("")
    enriched = pd.read_csv(OUTPUT / "professors_enriched.csv", dtype=str, encoding="utf-8-sig").fillna("")
    removed = pd.read_csv(DATA / "professor_overrides.csv", dtype=str, keep_default_na=False)
    removed_ids = set(removed.loc[(removed["column"] == "removed") & (removed["value"] == "yes"), "professor_id"])
    expected = set(manual["professor_id"]) - removed_ids
    assert expected - set(enriched["professor_id"]) == set()
    assert expected - set(nodes["professor_id"]) == set()


def test_every_hand_entered_lineage_link_is_on_the_map():
    # Hand-entered advisor/student links (relationships_manual.csv, verified) must stay in the output.
    links = pd.read_csv(DATA / "relationships_manual.csv", dtype=str, encoding="utf-8-sig").fillna("")
    links = links[(links["relationship_type"] == "advisor_student") & (links["verified"].str.lower() == "verified")]
    out = pd.read_csv(OUTPUT / "relationships.csv", dtype=str, encoding="utf-8-sig").fillna("")
    have = set(zip(out["professor_a_id"], out["professor_b_id"], out["relationship_type"]))
    missing = [(a, b) for a, b in zip(links["professor_a_id"], links["professor_b_id"])
               if (a, b, "advisor_student") not in have]
    assert missing == []


def test_hand_entered_names_are_not_overwritten():
    # The seed is the source for names: a manual row's Korean name must match the enriched list.
    manual = _manual_professors()
    enriched = pd.read_csv(OUTPUT / "professors_enriched.csv", dtype=str, encoding="utf-8-sig").fillna("")
    merged = manual.merge(enriched, on="professor_id", suffixes=("_seed", "_out"))
    changed = merged[(merged["name_ko_seed"] != "") & (merged["name_ko_seed"] != merged["name_ko_out"])]
    assert changed[["professor_id", "name_ko_seed", "name_ko_out"]].values.tolist() == []
