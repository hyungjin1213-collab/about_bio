import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import korean_names as kn  # noqa: E402


def test_pick_korean_needs_one_fitting_hangul_name():
    assert kn.pick_korean(["V. Narry Kim", "김빛내리", "Narry Kim"], "V. Narry Kim") == ""   # 빛내리 != Narry
    assert kn.pick_korean(["김대덕", "Kim Dae-Duk"], "Dae-Duk Kim") == "김대덕"
    assert kn.pick_korean(["김지원", "김지운"], "Jiwon Kim") == "김지원"
    assert kn.pick_korean(["이영희", "박민수"], "Jiwon Kim") == ""
    assert kn.pick_korean(["도시바"], "Toshiba Inc") == ""


class FakeOpenAlex:
    def __init__(self, alternatives):
        self.alternatives = alternatives

    def _get(self, path, params):
        ids = params["filter"].removeprefix("openalex:").split("|")
        return {"results": [{"id": f"https://openalex.org/{i}", "display_name_alternatives": self.alternatives.get(i, [])}
                            for i in ids]}


def test_fill_from_faculty_list_openalex_and_orcid(tmp_path, monkeypatch):
    monkeypatch.setattr(kn, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kn, "OUTPUT_DIR", tmp_path)
    pd.DataFrame([
        {"professor_id": "P1", "name_ko": "", "name_en": "Minsu Park", "university": "KIST", "openalex_id": "A1", "orcid": ""},
        {"professor_id": "P2", "name_ko": "", "name_en": "Jiwon Kim", "university": "KIST", "openalex_id": "A2", "orcid": ""},
        {"professor_id": "P3", "name_ko": "", "name_en": "Sora Kang", "university": "KIST", "openalex_id": "A3",
         "orcid": "0000-0001-0000-0003"},
        {"professor_id": "P4", "name_ko": "홍길동", "name_en": "Gildong Hong", "university": "KIST", "openalex_id": "A4", "orcid": ""},
    ]).to_csv(tmp_path / "professors_seed.csv", index=False)
    pd.DataFrame([{"name_ko": "박민수", "university": "KIST"}]).to_csv(tmp_path / "faculty_directory_v2.csv", index=False)

    class Session:
        def get(self, url, **kw):
            class R:
                status_code = 200

                def json(self_inner):
                    return {"name": {"given-names": {"value": "Sora"}, "family-name": {"value": "Kang"}},
                            "other-names": {"other-name": [{"content": "강소라"}]}}
            return R()

    n = kn.fill_korean_names(FakeOpenAlex({"A2": ["Ji-Won Kim", "김지원"]}), orcid_session=Session())
    seed = pd.read_csv(tmp_path / "professors_seed.csv", dtype=str).fillna("")
    assert n == 3
    assert seed.set_index("professor_id")["name_ko"].to_dict() == {"P1": "박민수", "P2": "김지원", "P3": "강소라", "P4": "홍길동"}
    log = pd.read_csv(tmp_path / "korean_name_fills.csv")
    assert set(log["source"]) == {"faculty list (same organisation)", "OpenAlex alternative name", "ORCID record"}
