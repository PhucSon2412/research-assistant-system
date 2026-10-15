"""Synthetic C4 drill accounts through real runtime writers, with no pending seam."""
import os
from sqlalchemy.orm import Session
from app.clock import utcnow
from app.config import get_settings
from app.ingest.runtime import require_runtime
from app.identity.bootstrap import bootstrap_admin
from app.identity.login import login
from app.identity.invitations import create_invitation, accept_invitation


def runtime_url(owner_url):
    user=os.environ.get("RAS_DRILL_RUNTIME_USER")
    password=os.environ.get("RAS_DRILL_RUNTIME_PASSWORD")
    if not user or not password or user==owner_url.username:
        raise RuntimeError("supply a separately provisioned drill runtime login; no owner fallback")
    return owner_url.set(username=user,password=password)


def seed_accounts(engine,emails,password,*,clock=utcnow):
    """One new-install bootstrap and real invitation acceptance for every User.

    Provision/migration belongs to the drill orchestration. This helper neither
    creates roles nor changes readiness, and never claims human verification.
    """
    with Session(engine) as db:
        require_runtime(db)
        db.rollback()  # no transaction across password hashing
    settings=get_settings()
    ids={"admin":bootstrap_admin(engine,email=emails["admin"],password=password,
                                 policy=settings.password,clock=clock).user_id}
    with Session(engine) as db:
        actor=login(db,email=emails["admin"],password=password,now=clock(),settings=settings,clock=clock)[1]
    for name,email in emails.items():
        if name=="admin":continue
        with Session(engine) as db:
            created=create_invitation(db,inviter_id=ids["admin"],actor_session_id=actor.session_id,
                csrf_token=actor.csrf_token,email=email,display_name=None,now=clock(),lifetime=settings.auth.invite,clock=clock)
        with Session(engine) as db:
            ids[name]=accept_invitation(db,raw_token=created.token,password=password,display_name=None,
                now=clock(),policy=settings.password,clock=clock).id
    return ids


def current_revision():
    from pathlib import Path
    from alembic.script import ScriptDirectory
    return ScriptDirectory(str(Path(__file__).resolve().parents[1]/"api"/"migrations")).get_current_head()
