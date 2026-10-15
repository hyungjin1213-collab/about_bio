from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"


def test_hand_edited_csvs_parse():
    # A stray comma in a memo broke the daily run (10/15): every data file must read cleanly.
    for path in sorted(DATA.glob("*.csv")) + sorted((DATA / "alumni_manual").glob("*.csv")):
        pd.read_csv(path, dtype=str)
