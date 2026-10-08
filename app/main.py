"""中文看板。标签来自 selection_reason、主题和实验室，不显示分数。"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, load_config
from app.db import FOCUS_REASONS, Database
from app.pipeline import run_sync
from app.presentation import PRESENTATION_PRIORITY

WEB_ROOT = Path(__file__).resolve().parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_ROOT / "templates"))

REASON_LABELS = {
    "conference_oral": "Oral",
    "conference_spotlight": "Spotlight",
    "conference_highlight": "Highlight",
    "conference_award": "Award",
    "rss_selective": "RSS 接收",
    "target_lab": "实验室",
    "target_topic": "主题",
    "awesome_list": "Awesome",
}

_sync_lock = threading.Lock()


def create_app(config: AppConfig | None = None, database: Database | None = None, schedule: bool = False) -> FastAPI:
    config = config or load_config()
    database = database or Database(config.database)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if schedule:
            scheduler = BackgroundScheduler()
            scheduler.add_job(
                lambda: _start_sync("incremental"),
                "interval",
                hours=config.track_interval_hours,
                id="incremental",
            )
            scheduler.add_job(
                lambda: _start_sync("reconcile"),
                "cron",
                hour=3,
                minute=15,
                id="reconcile",
            )
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="机器人操作论文雷达", lifespan=lifespan)
    app.state.config = config
    app.state.db = database
    app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")

    @app.get("/")
    def home(request: Request):
        return _render_list(request, {"group": "focus", "unread": "1"})

    @app.get("/papers")
    def papers(request: Request):
        filters = {
            "group": request.query_params.get("group", ""),
            "venue": request.query_params.get("venue", ""),
            "year": request.query_params.get("year", ""),
            "presentation": request.query_params.get("presentation", ""),
            "reason": request.query_params.get("reason", ""),
            "lab": request.query_params.get("lab", ""),
            "topic": request.query_params.get("topic", ""),
            "unread": request.query_params.get("unread", ""),
            "starred": request.query_params.get("starred", ""),
            "q": request.query_params.get("q", ""),
        }
        return _render_list(request, filters)

    @app.get("/papers/{paper_id}")
    def detail(request: Request, paper_id: int):
        paper = database.get_paper(paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="没有这篇论文")
        return TEMPLATES.TemplateResponse(
            request,
            "detail.html",
            {
                "paper": _decorate(paper, config),
                "nav": _nav(config),
                "stats": database.stats(),
            },
        )

    @app.post("/papers/{paper_id}/read")
    def mark_read(paper_id: int, next: str = "/"):
        updated = database.toggle_read(paper_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="没有这篇论文")
        if not next.startswith("/"):
            next = "/"
        return RedirectResponse(next, status_code=303)

    @app.post("/papers/{paper_id}/star")
    def mark_star(paper_id: int, next: str = "/"):
        updated = database.toggle_star(paper_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="没有这篇论文")
        if not next.startswith("/"):
            next = "/"
        return RedirectResponse(next, status_code=303)

    @app.get("/sync")
    def sync_page(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "sync.html",
            {
                "runs": database.recent_runs(),
                "cursors": database.cursors(),
                "nav": _nav(config),
                "stats": database.stats(),
                "busy": _sync_lock.locked(),
            },
        )

    @app.post("/sync")
    def sync_now(mode: str = "incremental"):
        if mode not in {"incremental", "reconcile", "backfill"}:
            mode = "incremental"
        started = _start_sync(mode)
        suffix = "started=1" if started else "busy=1"
        return RedirectResponse(f"/sync?{suffix}", status_code=303)

    return app


def _start_sync(mode: str) -> bool:
    if not _sync_lock.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            run_sync(mode)
        finally:
            _sync_lock.release()

    threading.Thread(target=_run, name=f"sync-{mode}", daemon=True).start()
    return True


def _render_list(request: Request, filters: dict):
    config: AppConfig = request.app.state.config
    database: Database = request.app.state.db
    papers = [_decorate(paper, config) for paper in database.list_papers(filters)]
    return TEMPLATES.TemplateResponse(
        request,
        "list.html",
        {
            "papers": papers,
            "filters": filters,
            "nav": _nav(config),
            "stats": database.stats(),
            "venues": database.venues(),
            "years": [2024, 2025, 2026],
            "presentations": list(PRESENTATION_PRIORITY),
            "reasons": REASON_LABELS,
            "labs": [lab for lab in config.labs if lab.enabled],
            "topics": config.topics,
            "heading": _heading(filters, config),
        },
    )


def _heading(filters: dict, config: AppConfig) -> str:
    group = filters.get("group") or ""
    if group == "focus":
        return "重点"
    if group:
        return config.topic_label(group)
    if filters.get("starred") == "1":
        return "收藏"
    if filters.get("lab"):
        return config.lab_label(filters["lab"])
    return "全部论文"


def _nav(config: AppConfig) -> list[dict]:
    items = [
        {"href": "/", "label": "重点"},
        {"href": "/papers?starred=1", "label": "收藏"},
        {"href": "/papers", "label": "全部"},
    ]
    for topic in config.topics:
        items.append({"href": f"/papers?group={topic.id}", "label": topic.label})
    for lab in config.labs:
        if lab.enabled:
            items.append({"href": f"/papers?lab={lab.id}", "label": lab.label})
    items.append({"href": "/sync", "label": "同步"})
    return items


def _decorate(paper: dict, config: AppConfig) -> dict:
    """把内部代码换成看板上的标签。重点不是分数。"""
    chips = []
    venue = paper.get("venue") or ""
    presentation = paper.get("presentation_type") or ""
    if venue or presentation:
        chips.append(" ".join(part for part in (venue, presentation.replace("_", " ")) if part).strip())
    for lab_id in paper.get("labs") or []:
        chips.append(config.lab_label(lab_id))
    for topic_id in paper.get("topics") or []:
        chips.append(config.topic_label(topic_id))
    for reason in paper.get("reasons") or []:
        if reason in {"target_lab", "target_topic"}:
            continue
        label = REASON_LABELS.get(reason, reason)
        if label not in chips:
            chips.append(label)
    paper["chips"] = chips
    paper["reason_labels"] = [REASON_LABELS.get(reason, reason) for reason in paper.get("reasons") or []]
    paper["topic_labels"] = [config.topic_label(topic) for topic in paper.get("topics") or []]
    paper["focus"] = _is_focus(paper)
    return paper


def _is_focus(paper: dict) -> bool:
    reasons = set(paper.get("reasons") or [])
    if reasons.intersection(FOCUS_REASONS):
        return True
    return "target_lab" in reasons and "target_topic" in reasons


app = create_app(schedule=True)
