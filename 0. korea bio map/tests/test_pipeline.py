import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pipeline  # noqa: E402


def _work(wid, *author_ids):
    return {"id": f"https://openalex.org/{wid}", "display_name": wid,
            "authorships": [{"author": {"id": f"https://openalex.org/{a}"}, "institutions": []} for a in author_ids]}


class FakeClient:
    works = {
        "A1": [_work("W1", "A1", "B1"), _work("W2", "A1", "C1"), _work("W9", *(["A1", "B1"] + [f"X{i}" for i in range(150)]))],
        "A9": [_work("W3", "A9", "B1")],   # second (split) profile of professor P1
        "B1": [_work("W1", "A1", "B1"), _work("W3", "A9", "B1")],
        "C1": [_work("W2", "A1", "C1")],
    }

    def iter_works_by_authors(self, author_ids):
        for aid in author_ids:
            yield from self.works.get(aid, [])


def test_collaborations_use_all_openalex_ids_and_skip_consortium_papers(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)
    professors = pd.DataFrame([
        {"professor_id": "P1", "openalex_id": "A1;A9"},
        {"professor_id": "P2", "openalex_id": "B1"},
        {"professor_id": "P3", "openalex_id": "C1"},
    ])
    auto, _ = pipeline.collect_collaborations(FakeClient(), professors)
    pairs = {(r.professor_a_id, r.professor_b_id): r.collaboration_paper_count for r in auto.itertuples()}
    # W1 + W3 (via split profile A9); W9 has 152 authors and is ignored.
    assert pairs == {("P1", "P2"): 2, ("P1", "P3"): 1}


def test_works_fetched_in_batches_not_per_professor(tmp_path, monkeypatch):
    """Run #24 used up OpenAlex's daily budget with one request per professor."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)
    calls = []

    class Counting:
        def iter_works_by_authors(self, ids):
            calls.append(len(ids))
            return iter([])

    professors = pd.DataFrame([{"professor_id": f"P{i}", "openalex_id": f"A{i}"} for i in range(120)])
    pipeline.collect_collaborations(Counting(), professors)
    assert calls == [50, 50, 20]


def test_lineage_scores_pi_and_trainee_differently(tmp_path, monkeypatch):
    out, data, web = tmp_path / "out", tmp_path / "data", tmp_path / "web"
    out.mkdir(); data.mkdir()
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", out)
    monkeypatch.setattr(pipeline, "DATA_DIR", data)
    monkeypatch.setattr(pipeline, "WEB_DIR", web)
    (data / "score_rules.csv").write_text(
        (Path(__file__).resolve().parents[1] / "data" / "score_rules.csv").read_text(encoding="utf-8-sig"))
    pd.DataFrame([
        {"relationship_id": "M1", "professor_a_id": "PI", "professor_b_id": "STU", "relationship_type": "advisor_student", "verified": "yes"},
        {"relationship_id": "M2", "professor_a_id": "PI", "professor_b_id": "PD", "relationship_type": "postdoc_mentor", "verified": "yes"},
        {"relationship_id": "M3", "professor_a_id": "PI", "professor_b_id": "X", "relationship_type": "advisor_student", "verified": ""},
    ]).to_csv(data / "relationships_manual.csv", index=False)
    pd.DataFrame([{"relationship_id": "AUTO1", "professor_a_id": "STU", "professor_b_id": "PD",
                   "relationship_type": "collaboration", "collaboration_paper_count": "3", "verified": "auto"}]
                 ).to_csv(out / "relationships_auto.csv", index=False)
    professors = pd.DataFrame([{"professor_id": p, "name_ko": p, "openalex_id": ""} for p in ["PI", "STU", "PD", "X"]])

    nodes, _ = pipeline.build_network(professors)
    score = dict(zip(nodes.professor_id, nodes.network_score))
    # PI: 1 student (10) + 1 postdoc (3); unverified M3 ignored
    # STU: advisor (1) + 1 collaborator (1); PD: mentor (1) + 1 collaborator (1)
    assert score == {"PI": 13, "STU": 2, "PD": 2, "X": 0}


def test_collaboration_weight():
    w = pipeline.collaboration_weight
    assert w(3, 2026, False, now=2026) == 0.5                      # small team paper
    assert round(w(91, 2026, False, now=2026), 3) == 0.011          # consortium paper
    assert w(3, 2026, True, now=2026) == 0.75                       # both senior authors
    assert w(3, 2024, False, now=2026) == 0.5                       # last 2 years: full weight
    assert round(w(3, 2016, False, now=2026), 3) == 0.25            # 10 years old: halved


def test_weak_coauthorships_are_not_drawn(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)

    def work(wid, n_authors, *profs, year=2025):
        authors = [{"author": {"id": p}, "author_position": "middle"} for p in profs]
        authors += [{"author": {"id": f"X{wid}{i}"}, "author_position": "middle"} for i in range(n_authors - len(profs))]
        return {"id": wid, "publication_year": year, "display_name": wid, "authorships": authors}

    works = [
        work("W1", 3, "A", "B"),                                  # A-B: one small paper -> drawn
        work("W2", 40, "A", "C"),                                 # A-C: one big paper -> not drawn
        work("W3", 40, "C", "D"), work("W4", 40, "C", "D"),       # C-D: two big papers -> drawn (repeat)
        *[work(f"W{i}", 6, "B", "C") for i in range(5, 8)],      # B-C: three 6-author papers -> drawn
    ]

    class Client:
        def iter_works_by_authors(self, ids):
            return iter(works)

    professors = pd.DataFrame([{"professor_id": p, "openalex_id": p} for p in "ABCD"])
    auto, _ = pipeline.collect_collaborations(Client(), professors)
    got = {(r.professor_a_id, r.professor_b_id): r.collaboration_paper_count for r in auto.itertuples()}
    assert got == {("A", "B"): 1, ("B", "C"): 3, ("C", "D"): 2}


def test_author_stats_add_up_split_profiles(tmp_path, monkeypatch):
    import pandas as pd
    import pipeline

    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "AUTHOR_STATS_PATH", tmp_path / "author_stats.csv")
    year = pipeline.date.today().year

    class Client:
        def _get(self, path, params):
            assert "A1|A2" in params["filter"]
            if path == "works":   # articles and reviews only, grouped by author
                recent = "publication_year" in params["filter"]
                return {"group_by": [{"key": "https://openalex.org/A1", "count": 4 if recent else 60},
                                     {"key": "https://openalex.org/A2", "count": 1 if recent else 3},
                                     {"key": "https://openalex.org/A9", "count": 99}]}
            return {"results": [
                {"id": "https://openalex.org/A1", "works_count": 80, "cited_by_count": 3900,
                 "summary_stats": {"h_index": 30}, "counts_by_year": [{"year": year, "works_count": 5, "cited_by_count": 400},
                                                                      {"year": year - 9, "works_count": 9, "cited_by_count": 10}]},
                {"id": "https://openalex.org/A2", "works_count": 5, "cited_by_count": 44,
                 "summary_stats": {"h_index": 3}, "counts_by_year": [{"year": year - 1, "works_count": 2, "cited_by_count": 4}]}]}

    out = pipeline.collect_author_stats(Client(), pd.DataFrame([{"professor_id": "P1", "openalex_id": "A1;A2"}]))
    r = out.iloc[0]
    assert (r["papers"], r["citations"], r["h_index"], r["recent_papers"], r["recent_citations"]) == (63, 3944, 30, 5, 404)


def test_removed_override_drops_professor():
    from pipeline import apply_professor_overrides
    profs = pd.DataFrame([{"professor_id": "P1", "name_ko": "가"}, {"professor_id": "P2", "name_ko": "나"}])
    ov = pd.DataFrame([{"professor_id": "P1", "column": "removed", "value": "yes", "memo": "은퇴"}])
    assert list(apply_professor_overrides(profs, ov)["professor_id"]) == ["P2"]


def test_retired_status_keeps_professor_on_map():
    from pipeline import apply_professor_overrides, removed_professors, retired_professors
    profs = pd.DataFrame([{"professor_id": "P1", "name_ko": "가"}, {"professor_id": "P2", "name_ko": "나"}])
    ov = pd.DataFrame([{"professor_id": "P1", "column": "status", "value": "retired", "memo": "은퇴"}])
    assert retired_professors(ov) == {"P1"}
    assert removed_professors(ov) == set()
    assert list(apply_professor_overrides(profs, ov)["professor_id"]) == ["P1", "P2"]


def test_typed_author_stats_replace_openalex_figures(tmp_path, monkeypatch):
    import pandas as pd
    from pipeline import AUTHOR_STATS_COLUMNS, _with_manual_stats
    manual = tmp_path / "author_stats_manual.csv"
    manual.write_text("professor_id,papers,citations,h_index,recent_papers,recent_citations\nP1,260,16887,76,69,7883\n", encoding="utf-8")
    monkeypatch.setattr(pipeline, "AUTHOR_STATS_MANUAL_PATH", manual)
    out = pd.DataFrame([{"professor_id": "P1", "papers": 1, "citations": 2, "h_index": 3,
                         "recent_papers": 4, "recent_citations": 5},
                        {"professor_id": "P2", "papers": 6, "citations": 7, "h_index": 8,
                         "recent_papers": 9, "recent_citations": 10}], columns=AUTHOR_STATS_COLUMNS)
    merged = _with_manual_stats(out).set_index("professor_id")
    assert list(merged.loc["P1"]) == [260, 16887, 76, 69, 7883]
    assert merged.loc["P2", "papers"] == 6


def test_blank_manual_figure_keeps_openalex_value(tmp_path, monkeypatch):
    import pandas as pd
    from pipeline import AUTHOR_STATS_COLUMNS, _with_manual_stats
    manual = tmp_path / "author_stats_manual.csv"
    manual.write_text("professor_id,papers,citations,h_index,recent_papers,recent_citations\nP1,,32084,97,,13862\n", encoding="utf-8")
    monkeypatch.setattr(pipeline, "AUTHOR_STATS_MANUAL_PATH", manual)
    out = pd.DataFrame([{"professor_id": "P1", "papers": 7, "citations": 1, "h_index": 2,
                         "recent_papers": 4, "recent_citations": 5}], columns=AUTHOR_STATS_COLUMNS)
    row = _with_manual_stats(out).set_index("professor_id").loc["P1"]
    assert (row["papers"], row["citations"], row["h_index"], row["recent_papers"], row["recent_citations"]) == (7, 32084, 97, 4, 13862)
