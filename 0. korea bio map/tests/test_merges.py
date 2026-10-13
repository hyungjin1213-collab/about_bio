import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipeline import apply_professor_merges, apply_professor_overrides  # noqa: E402


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


def test_overrides_set_columns_and_ignore_unknown():
    profs = pd.DataFrame([{"professor_id": "P1", "name_ko": "", "department": ""}])
    fixes = pd.DataFrame([{"professor_id": "P1", "column": "name_ko", "value": "정교수"},
                          {"professor_id": "P1", "column": "nonexistent", "value": "x"},
                          {"professor_id": "P2", "column": "name_ko", "value": "없음"}])
    out = apply_professor_overrides(profs, fixes)
    assert out.iloc[0]["name_ko"] == "정교수" and list(out.columns) == ["professor_id", "name_ko", "department"]


def test_openalex_from_scholar_needs_name_fit_and_two_papers():
    from pipeline import openalex_from_scholar

    class Scholar:
        def author_article_titles(self, sid):
            return ["Paper one", "Paper two", "Paper three"]

    class Client:
        def _get(self, path, params):
            works = {"Paper one": ["Sangwoo Seo", "Other Person"], "Paper two": ["Sang Woo Seo"],
                     "Paper three": ["Someone Else"]}[params["search"]]
            ids = {"Sangwoo Seo": "A1", "Sang Woo Seo": "A1", "Other Person": "A2", "Someone Else": "A3"}
            return {"results": [{"authorships": [{"author": {"id": f"https://openalex.org/{ids[n]}",
                                                             "display_name": n}} for n in works]}]}

    assert openalex_from_scholar(Client(), Scholar(), "X", "서상우", "") == "A1"
    assert openalex_from_scholar(Client(), Scholar(), "X", "김원종", "") == ""      # nobody fits the name
