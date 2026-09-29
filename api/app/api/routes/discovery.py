"""Public-to-authenticated discovery metadata; isolated from library ACL."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, require_admin
from app.authz import Principal
from app.models.discovery import DiscoveryPaper, DiscoveryPaperTopic, DiscoverySource

router = APIRouter(prefix="/discovery", tags=["discovery"])


@router.get("/topics")
def list_topics(_: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return {"topics": db.scalars(select(DiscoverySource.topic).order_by(DiscoverySource.topic)).all()}


@router.get("/papers")
def list_papers(topic: str | None = None,
                limit: int = Query(default=20, ge=1, le=100),
                offset: int = Query(default=0, ge=0, le=100_000),
                _: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    where = DiscoveryPaper.content_status == "metadata_only"
    ids = select(DiscoveryPaper.id).where(where)
    if topic:
        ids = ids.join(DiscoveryPaperTopic, DiscoveryPaperTopic.paper_id == DiscoveryPaper.id).where(DiscoveryPaperTopic.topic == topic)
    ids = ids.subquery()
    total = db.scalar(select(func.count()).select_from(ids)) or 0
    papers = db.scalars(select(DiscoveryPaper).join(ids, DiscoveryPaper.id == ids.c.id)
                        .order_by(DiscoveryPaper.first_discovered_at.desc(), DiscoveryPaper.id.desc())
                        .limit(limit).offset(offset)).all()
    paper_ids = [p.id for p in papers]
    topics: dict = {paper_id: [] for paper_id in paper_ids}
    if paper_ids:
        for paper_id, value in db.execute(select(DiscoveryPaperTopic.paper_id, DiscoveryPaperTopic.topic)
                                          .where(DiscoveryPaperTopic.paper_id.in_(paper_ids))).all():
            topics[paper_id].append(value)
    return {"items": [{"id": str(p.id), "source": p.source, "arxiv_id": p.remote_id,
                       "original_url": p.original_url, "title": p.title, "authors": p.authors,
                       "abstract": p.abstract, "categories": p.categories, "topics": sorted(topics[p.id]),
                       "latest_version": p.latest_version, "source_published_at": p.source_published_at,
                       "source_updated_at": p.source_updated_at, "first_discovered_at": p.first_discovered_at,
                       "content_status": p.content_status} for p in papers],
            "total": total, "limit": limit, "offset": offset, "topic": topic}


@router.get("/sources")
def source_status(_: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(DiscoverySource).order_by(DiscoverySource.topic)).all()
    return {"sources": [{"topic": r.topic, "state": r.state, "window_from": r.window_from,
                         "window_until": r.window_until, "has_resumption_token": bool(r.resumption_token),
                         "next_attempt_at": r.next_attempt_at, "last_attempt_at": r.last_attempt_at,
                         "last_success_at": r.last_success_at, "last_error": r.last_error,
                         "consecutive_errors": r.consecutive_errors, "pages_seen": r.pages_seen,
                         "records_seen": r.records_seen} for r in rows]}
