"""收录规则的夹具测试。不访问网络。"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import load_config
from app.db import Database
from app.main import create_app
from app.matching import match_labs, section_allowed
from app.models import Author, Candidate, Institution
from app.pipeline import ingest_candidates
from app.presentation import upgrade_presentation
from app.selector import select
from app.sources.awesome import parse_awesome_markdown
from app.sources.openreview_source import next_failure_state, openreview_is_paused
from app.sources.programs import candidates_from_program_html
from app.sources.rss import candidates_from_rss
from app.sources.virtual_json import candidates_from_virtual, presentation_from_virtual


def _config():
    return load_config()


def _candidate(**kwargs) -> Candidate:
    defaults = dict(
        title="Untitled",
        source="test",
        source_id="1",
        kind="arxiv",
        categories=["cs.RO"],
        published_at="2025-06-01",
    )
    defaults.update(kwargs)
    return Candidate(**defaults)


def test_award_does_not_change_presentation_rank():
    assert upgrade_presentation("spotlight", "award") == "spotlight"
    assert upgrade_presentation("poster", "oral") == "oral"
    assert upgrade_presentation("oral", "poster") == "oral"
    presentation, award = presentation_from_virtual(
        "Accept (spotlight); best paper award",
        "Poster",
        "Spotlight Poster",
    )
    assert presentation == "spotlight"
    assert award is True


def test_virtual_oral_kept_poster_dropped_and_lab_poster_kept(tmp_path):
    config = _config()
    db = Database(tmp_path / "papers.db")
    payload = {
        "results": [
            {
                "id": 1,
                "name": "An Oral Method",
                "decision": "Accept (oral)",
                "eventtype": "Oral",
                "event_type": "Oral",
                "abstract": "nothing topical",
                "authors": [{"fullname": "Ada Lovelace", "institution": "MIT"}],
            },
            {
                "id": 2,
                "name": "A Poster Method",
                "decision": "Accept (poster)",
                "eventtype": "Poster",
                "event_type": "Poster",
                "abstract": "nothing topical",
                "authors": [{"fullname": "Grace Hopper", "institution": "MIT"}],
            },
            {
                "id": 3,
                "name": "Lab Poster",
                "decision": "Accept (poster)",
                "eventtype": "Poster",
                "event_type": "Poster",
                "abstract": "nothing topical",
                "authors": [{"fullname": "Ken Thompson", "institution": "NVIDIA Research"}],
            },
            {
                "id": 4,
                "name": "Spotlight With Award",
                "decision": "Accept (spotlight) award",
                "eventtype": "Poster",
                "event_type": "Spotlight Poster",
                "abstract": "nothing topical",
                "authors": [{"fullname": "Edsger Dijkstra", "institution": "UT Austin"}],
            },
        ]
    }
    counts = ingest_candidates(
        db,
        config,
        candidates_from_virtual(payload, "CVPR", 2026, "https://example.test/cvpr.json"),
    )
    assert counts["dropped"] == 1
    titles = {paper["title"]: paper for paper in db.list_papers({})}
    assert set(titles) == {"An Oral Method", "Lab Poster", "Spotlight With Award"}
    assert titles["An Oral Method"]["presentation_type"] == "oral"
    assert "conference_oral" in titles["An Oral Method"]["reasons"]
    assert titles["Lab Poster"]["presentation_type"] == "poster"
    assert titles["Lab Poster"]["reasons"] == ["target_lab"]
    assert "nvidia" in titles["Lab Poster"]["labs"]
    award = titles["Spotlight With Award"]
    assert award["presentation_type"] == "spotlight"
    assert "conference_award" in award["reasons"]
    assert "conference_spotlight" in award["reasons"]
    assert "award" != award["presentation_type"]


def test_rss_is_spotlight_not_conference_oral():
    html = """
    <table><tr session="1. Perception and Navigation">
      <td>1</td><td>Perception and Navigation</td>
      <td><a href="/2025/program/papers/1/"><b>Learned Perceptive Forward Dynamics Model</b></a></td>
      <td>Pascal Roth, Jonas Frey</td>
    </tr></table>
    """
    candidates = candidates_from_rss(html, 2025, "https://roboticsconference.org/2025/program/papers/", "spotlight")
    decision = select(candidates[0], _config())
    assert decision.keep
    assert decision.presentation_type == "spotlight"
    assert decision.reasons == ["rss_selective"]
    assert "conference_oral" not in decision.reasons


def test_topic_rules_do_not_require_a_lab(tmp_path):
    config = _config()
    db = Database(tmp_path / "papers.db")
    kept = _candidate(
        title="Dexterous Manipulation with Visuomotor Policies",
        abstract="A university lab studies grasping.",
        authors=[Author("Yan LeCun", [Institution("New York University")])],
        arxiv_id="2501.00001",
        source_id="2501.00001",
    )
    dropped_ml = _candidate(
        title="Deep Reinforcement Learning for Large Language Models",
        abstract="We tune language models.",
        categories=["cs.LG"],
        arxiv_id="2501.00002",
        source_id="2501.00002",
    )
    dropped_weak = _candidate(
        title="Reinforcement Learning for Control",
        abstract="We optimize a controller.",
        arxiv_id="2501.00003",
        source_id="2501.00003",
    )
    weak_kept = _candidate(
        title="Reinforcement Learning for Control",
        abstract="robot learning for manipulation and grasping",
        arxiv_id="2501.00004",
        source_id="2501.00004",
    )
    counts = ingest_candidates(db, config, [kept, dropped_ml, dropped_weak, weak_kept])
    assert counts["dropped"] == 2
    papers = {paper["title"]: paper for paper in db.list_papers({})}
    assert "dexterous_tactile" in papers[kept.title]["topics"]
    assert "manipulation" in papers[kept.title]["topics"]
    assert "target_topic" in papers[kept.title]["reasons"]
    assert papers[kept.title]["labs"] == []
    assert "robot_learning" in papers[weak_kept.title]["topics"]


def test_institution_alias_is_token_based():
    config = _config()
    assert match_labs(config, [Institution("NVIDIA Research")]) == ["nvidia"]
    assert match_labs(config, [Institution("TRI")]) == ["tri"]
    assert match_labs(config, [Institution("TRIangle Labs")]) == []
    assert match_labs(config, [Institution("Toyota Research Institute")]) == ["tri"]


def test_awesome_sections_keep_vla_and_skip_vln(tmp_path):
    config = _config()
    markdown = """
## Vision-Language-Action
- **Some Unusual Title Without Keywords** [arXiv](https://arxiv.org/abs/2502.00001)
## VLN Models
- **Navigation Baseline For Hallways** [arXiv](https://arxiv.org/abs/2502.00002)
## Simulation
- **A New Simulator** [arXiv](https://arxiv.org/abs/2502.00003)
"""
    assert section_allowed(config, ["Vision-Language-Action"])
    assert not section_allowed(config, ["VLN Models"])
    candidates = parse_awesome_markdown(markdown, "demo/list", "https://example.test/readme")
    db = Database(tmp_path / "papers.db")
    ingest_candidates(db, config, candidates)
    papers = db.list_papers({})
    assert [paper["title"] for paper in papers] == ["Some Unusual Title Without Keywords"]
    assert "awesome_list" in papers[0]["reasons"]
    assert "vla" in papers[0]["topics"]


def test_merge_upgrades_presentation_and_keeps_old_reasons(tmp_path):
    config = _config()
    db = Database(tmp_path / "papers.db")
    first = _candidate(
        title="Dexterous Cable Routing",
        abstract="robotic manipulation of deformable cables",
        authors=[Author("Ada Lovelace")],
        doi="10.1000/cable",
        arxiv_id="2503.00001",
        source="arxiv",
        source_id="2503.00001",
        presentation_type="preprint",
        venue="arXiv",
        published_at="2025-04-01",
    )
    ingest_candidates(db, config, [first])
    second = _candidate(
        title="Dexterous Cable Routing",
        abstract="robotic manipulation of deformable cables",
        authors=[Author("Ada Lovelace", [Institution("NVIDIA Research")])],
        doi="10.1000/cable",
        venue="CVPR",
        conference_year=2026,
        presentation_type="oral",
        kind="conference",
        source="virtual:CVPR",
        source_id="99",
        categories=[],
        published_at="2026-06-01",
    )
    ingest_candidates(db, config, [second])
    papers = db.list_papers({})
    assert len(papers) == 1
    paper = db.get_paper(papers[0]["id"])
    assert paper["presentation_type"] == "oral"
    assert "target_topic" in paper["reasons"]
    assert "conference_oral" in paper["reasons"]
    assert "target_lab" in paper["reasons"]
    assert {item["source"] for item in paper["sources"]} == {"arxiv", "virtual:CVPR"}
    assert paper["arxiv_published_at"] == "2025-04-01"
    assert paper["conference_published_at"] == "2026-06-01"


def test_title_merge_allows_one_year_gap_only(tmp_path):
    config = _config()
    db = Database(tmp_path / "papers.db")
    base = _candidate(
        title="Contact Rich Insertion",
        abstract="contact-rich assembly",
        authors=[Author("Barbara Liskov")],
        presentation_type="preprint",
        published_at="2025-01-15",
        arxiv_id="2504.00001",
        source_id="2504.00001",
    )
    ingest_candidates(db, config, [base])
    later = _candidate(
        title="Contact Rich Insertion",
        abstract="contact-rich assembly",
        authors=[Author("Barbara Liskov")],
        kind="conference",
        venue="CoRL",
        conference_year=2026,
        presentation_type="spotlight",
        source="program:CoRL",
        source_id="later",
        published_at="2026-11-01",
        categories=[],
    )
    # 2026-11-01 超出时间窗，但会议记录不按 arXiv 窗口丢弃。
    ingest_candidates(db, config, [later])
    assert len(db.list_papers({})) == 1
    far = _candidate(
        title="Contact Rich Insertion",
        abstract="contact-rich assembly",
        authors=[Author("Barbara Liskov")],
        kind="conference",
        venue="RSS",
        presentation_type="oral",
        published_at="2023-07-01",
        conference_year=2023,
        source="program:RSS",
        source_id="far",
        categories=[],
    )
    # 年份差为 2，不能只靠标题和第一作者合并，所以会另存一行。
    ingest_candidates(db, config, [far])
    assert len(db.list_papers({})) == 2


def test_openreview_pauses_after_repeated_failures():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    failures = 0
    disabled = None
    state = "error"
    for _ in range(4):
        failures, disabled, state = next_failure_state(failures, 5, 24, now)
    assert state == "error"
    assert disabled is None
    failures, disabled, state = next_failure_state(failures, 5, 24, now)
    assert state == "paused"
    assert disabled is not None
    assert openreview_is_paused({"disabled_until": disabled}, now.isoformat())
    later = (now + timedelta(hours=25)).isoformat()
    assert not openreview_is_paused({"disabled_until": disabled}, later)


def test_program_page_requires_an_explicit_signal():
    html = """
    <a href="https://arxiv.org/abs/2505.00001">A Paper Sitting In The Program Without A Label</a>
    <p>oral session</p>
    <a href="https://arxiv.org/abs/2505.00002">Explicit Oral Paper About Nothing In Particular</a>
    """
    candidates = candidates_from_program_html(html, "ICRA", 2025, "https://example.test/icra")
    assert len(candidates) == 1
    assert candidates[0].presentation_type == "oral"
    assert candidates[0].kind == "conference"


def test_dashboard_filters_detail_and_read_state(tmp_path):
    config = _config()
    database = Database(tmp_path / "papers.db")
    ingest_candidates(
        database,
        config,
        [
            _candidate(
                title="Dexterous Manipulation Benchmark",
                abstract="university grasping study",
                arxiv_id="2506.00001",
                source_id="2506.00001",
                authors=[Author("Ada Lovelace")],
            )
        ],
    )
    app = create_app(config=config, database=database, schedule=False)
    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert "重点" in home.text
    # 只有主题、没有实验室，不算重点。首页默认是未读重点，所以这篇不出现。
    assert "还没有论文" in home.text
    listing = client.get("/papers?topic=dexterous_tactile")
    assert listing.status_code == 200
    assert "Dexterous Manipulation Benchmark" in listing.text
    paper_id = database.list_papers({})[0]["id"]
    detail = client.get(f"/papers/{paper_id}")
    assert detail.status_code == 200
    assert "为什么在雷达里" in detail.text
    marked = client.post(f"/papers/{paper_id}/read?next=/papers/{paper_id}", follow_redirects=True)
    assert marked.status_code == 200
    assert "标为未读" in marked.text
    assert database.get_paper(paper_id)["is_read"] == 1
    starred = client.post(f"/papers/{paper_id}/star?next=/papers/{paper_id}", follow_redirects=True)
    assert starred.status_code == 200
    assert "取消收藏" in starred.text
    assert database.get_paper(paper_id)["is_starred"] == 1
    shelf = client.get("/papers?starred=1")
    assert "Dexterous Manipulation Benchmark" in shelf.text
    client.post(f"/papers/{paper_id}/star?next=/papers?starred=1", follow_redirects=True)
    assert database.get_paper(paper_id)["is_starred"] == 0
    assert "Dexterous Manipulation Benchmark" not in client.get("/papers?starred=1").text
