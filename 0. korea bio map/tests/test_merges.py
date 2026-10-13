import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipeline import apply_professor_merges  # noqa: E402


def test_merge_folds_profiles_and_fills_blanks():
    profs = pd.DataFrame([
        {"professor_id": "P1", "name_ko": "조교수", "name_en": "Gyo Su Jo", "university": "A University",
         "openalex_id": "", "orcid": ""},
        {"professor_id": "P2", "name_ko": "", "name_en": "Gyo-Su Jo", "university": "B Institute",
         "openalex_id": "A1", "orcid": "0000-0001"},
        {"professor_id": "P3", "name_ko": "", "name_en": "Other", "university": "C", "openalex_id": "A3", "orcid": ""},
    ])
    merges = pd.DataFrame([{"keep_id": "P1", "merge_id": "P2"}, {"keep_id": "P9", "merge_id": "P3"}])
    out = apply_professor_merges(profs, merges)
    assert list(out["professor_id"]) == ["P1", "P3"]                      # unknown keep_id: nothing dropped
    p1 = out.iloc[0]
    assert (p1["name_ko"], p1["university"], p1["openalex_id"], p1["orcid"]) == ("조교수", "A University", "A1", "0000-0001")
    assert apply_professor_merges(out, merges).equals(out)                 # applying again changes nothing
