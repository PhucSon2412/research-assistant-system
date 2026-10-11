"""Short runtime transactions shared by HTTP search and its financial checkpoints."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth_safety
from app.api.errors import ApiError
from app.ingest import runtime


def release(db):
    try:
        runtime.release(db)
    except runtime.Halt:
        raise ApiError(503, 'search_release_unavailable') from None


def checkpoint(engine, phase, operation):
    db = Session(engine, expire_on_commit=False)
    outcome = runtime.Outcome()
    db.info['search_phase'] = phase
    try:
        result = operation(db, outcome)
    except BaseException as exc:
        try:
            release(db)
        except ApiError:
            pass
        if isinstance(exc, ApiError):
            raise
        if isinstance(exc, runtime.Halt):
            raise ApiError(503, exc.code.replace('worker_', 'search_')) from None
        if not isinstance(exc, Exception):
            raise
        raise ApiError(503, 'search_checkpoint_unavailable') from None
    release(db)
    return result


def commit(db, outcome):
    try:
        runtime.commit(db, outcome)
    except runtime.Halt:
        raise ApiError(503, 'search_commit_uncertain') from None


def entry(engine):
    def check(db, _):
        runtime.require_runtime(db)
        missing = db.scalar(text("""SELECT EXISTS (
            SELECT 1 FROM unnest(ARRAY['search_usage','search_request_days']) t(name)
            CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE']) p(name)
            WHERE NOT has_table_privilege(t.name,p.name))
            OR NOT has_table_privilege('library_items','UPDATE')
            OR NOT has_table_privilege('collections','UPDATE') OR EXISTS (
            SELECT 1 FROM unnest(ARRAY['library_items','paper_versions','source_files','chunks',
                'sections','index_generations','index_entries','embedding_calls','embedding_budget_days',
                'embedding_user_budget_days','collections','collection_items','access_rights',
                'common_publications','deletion_tombstones','ingestion_jobs','sessions','users']) t(name)
            WHERE NOT has_table_privilege(t.name,'SELECT'))"""))
        if missing is not False:
            raise ApiError(503, 'search_runtime_grants_missing')
        auth_safety.require_writer(db)
    return checkpoint(engine, 'entry', check)
