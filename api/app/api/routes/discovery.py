"""Discovery metadata for authenticated users; isolated from library ACL.

The one library-facing route, ``citing-items``, returns only the caller's own
private items whose extracted references link to the paper; the private
derivative gate is applied in SQL before counting and pagination.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, require_admin
from app.api.errors import ApiError
from app.authz import Principal, m2a_private_derivatives_clause
from app.citations import RELATION_NOTE, fresh_linked_clause, hidden_discovery_clause
from app.models import LibraryItem, ReferenceCitationLink, ReferenceEntry
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
    ids = select(DiscoveryPaper.id).where(DiscoveryPaper.content_status == "metadata_only", ~hidden_discovery_clause())
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


@router.get("/papers/{paper_id}/citing-items")
def citing_items(paper_id: str,
                 limit: int = Query(default=20, ge=1, le=100),
                 offset: int = Query(default=0, ge=0, le=100_000),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    try:
        parsed = uuid.UUID(paper_id)
    except ValueError:
        raise ApiError(404, "not_found") from None
    paper = db.scalar(select(DiscoveryPaper).where(
        DiscoveryPaper.id == parsed, DiscoveryPaper.content_status == "metadata_only", ~hidden_discovery_clause()))
    if paper is None:
        raise ApiError(404, "not_found")
    # Every filter, including the caller's private-derivative gate, precedes count/limit.
    edges = (select(ReferenceEntry.item_id, ReferenceEntry.id.label("reference_id"))
             .join(ReferenceCitationLink, ReferenceCitationLink.reference_id == ReferenceEntry.id)
             .join(DiscoveryPaper, DiscoveryPaper.id == ReferenceCitationLink.discovery_paper_id)
             .join(LibraryItem, LibraryItem.id == ReferenceEntry.item_id)
             .where(ReferenceCitationLink.discovery_paper_id == paper.id, fresh_linked_clause(),
                    m2a_private_derivatives_clause(principal))).subquery()
    total = db.scalar(select(func.count(func.distinct(edges.c.item_id)))) or 0
    items = db.execute(select(LibraryItem.id, LibraryItem.title, func.count(edges.c.reference_id))
                       .join(edges, edges.c.item_id == LibraryItem.id)
                       .group_by(LibraryItem.id, LibraryItem.title, LibraryItem.created_at)
                       .order_by(LibraryItem.created_at.desc(), LibraryItem.id)
                       .limit(limit).offset(offset)).all()
    return {"discovery_paper": {"id": str(paper.id), "arxiv_id": paper.remote_id, "title": paper.title,
                                "original_url": paper.original_url, "content_status": paper.content_status,
                                "label": "metadata-only"},
            "items": [{"id": str(item_id), "title": title, "linked_references": count}
                      for item_id, title, count in items],
            "total": total, "limit": limit, "offset": offset, "relation": "cites",
            "citation_relation_note": RELATION_NOTE}


@router.get("/sources")
def source_status(_: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(DiscoverySource).order_by(DiscoverySource.topic)).all()
    return {"sources": [{"topic": r.topic, "state": r.state, "window_from": r.window_from,
                         "window_until": r.window_until, "has_resumption_token": bool(r.resumption_token),
                         "next_attempt_at": r.next_attempt_at, "last_attempt_at": r.last_attempt_at,
                         "last_success_at": r.last_success_at, "last_error": r.last_error,
                         "consecutive_errors": r.consecutive_errors, "pages_seen": r.pages_seen,
                         "records_seen": r.records_seen} for r in rows]}
