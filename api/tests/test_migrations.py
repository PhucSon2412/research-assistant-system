from __future__ import annotations

import io
from decimal import Decimal

import pytest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base
from tests.conftest import HEAD_REVISION, alembic_config, upgrade

EXPECTED_TABLES = {
    "alembic_version",
    "users",
    "bootstrap_state",
    "library_items",
    "paper_versions",
    "source_files",
    "ingestion_jobs",
    "audit_events",
    # M1b (0002)
    "invitations",
    "sessions",
    "password_reset_tokens",
    "processing_acknowledgements",
    "upload_intents",
    "ingest_step_attempts",
    "metadata_candidates",
    "sections",
    "chunks",
    "pdf_pages",
    "reference_entries",
    "index_generations", "embedding_calls", "index_entries", "embedding_budget_days",
    "embedding_user_budget_days",
    "search_request_days", "search_usage", "rag_request_days",
    "collections", "collection_items",
    "discovery_sources", "discovery_papers", "discovery_paper_topics", "discovery_request_clock",
    "discovery_paper_licenses", "discovery_deleted_records",
    "reference_citation_links",
    "review_requests",
    "access_rights",
    "common_publications", "deletion_tombstones",
}


def _tables(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_empty_database_upgrades_to_head(empty_db_url: str) -> None:
    assert _tables(empty_db_url) == set()

    upgrade(empty_db_url)

    assert _tables(empty_db_url) == EXPECTED_TABLES
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")) == 1
        # No seeded/default accounts or credentials.
        assert conn.scalar(text("SELECT count(*) FROM users")) == 0
        assert conn.scalar(text("SELECT count(*) FROM bootstrap_state")) == 0
        assert conn.scalar(text("SELECT count(*) FROM audit_events")) == 0
    eng.dispose()


def test_rerunning_upgrade_is_a_noop(migrated_db_url: str) -> None:
    upgrade(migrated_db_url)
    assert _tables(migrated_db_url) == EXPECTED_TABLES


def test_downgrade_to_base_and_upgrade_again(migrated_db_url: str) -> None:
    command.downgrade(alembic_config(migrated_db_url), "base")
    assert _tables(migrated_db_url) == {"alembic_version"}
    eng = create_engine(migrated_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'ras\\_%'")) == 0
    eng.dispose()

    upgrade(migrated_db_url)
    assert _tables(migrated_db_url) == EXPECTED_TABLES


def test_orm_models_match_migrated_schema(migrated_db_url: str) -> None:
    eng = create_engine(migrated_db_url)
    with eng.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        diff = compare_metadata(ctx, Base.metadata)
    eng.dispose()
    assert diff == []


def test_0018_preserves_existing_license_provenance(empty_db_url: str) -> None:
    upgrade(empty_db_url, "0017")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        paper_id = conn.scalar(text("""INSERT INTO discovery_papers
            (remote_id, original_url, title, latest_version, license_url, oai_datestamp)
            VALUES ('2609.00001', 'https://arxiv.org/abs/2609.00001', 'Before upgrade', 'v2',
                    'http://arxiv.org/licenses/nonexclusive-distrib/1.0/', DATE '2026-09-28')
            RETURNING id"""))
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT version_label,license_url FROM discovery_paper_licenses WHERE paper_id=:p"),
                           {"p": paper_id}).one()
        assert row == ("v2", "http://arxiv.org/licenses/nonexclusive-distrib/1.0/")
    eng.dispose()


def test_0019_keeps_existing_references_and_backfill_links_them(empty_db_url: str) -> None:
    from app.citations import reconcile_pending
    from app.config import CitationPolicy
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0018")
    eng = create_engine(empty_db_url)
    raw = "[1] Existing. arXiv:2609.00041v2."
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m5b@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M5a item') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,version_id,idempotency_key,state,rights_epoch_at_start) VALUES('upload','private',:o,:i,:v,'m5a-existing','awaiting_index',0) RETURNING id"),
                          {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,job_id,storage_key,verified_mime,byte_size,checksum_sha256) VALUES(:i,:v,:j,'existing-md','text/markdown',100,:h) RETURNING id"),
                             {"i": item, "v": version, "j": job, "h": "b" * 64})
        conn.execute(text("INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,raw_citation,fields,confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version) VALUES(:i,:v,:f,0,:r,'{}','raw_only','markdown-v1','markdown',0,:n,'markdown-v1')"),
                     {"i": item, "v": version, "f": source, "r": raw, "n": len(raw)})
        conn.execute(text("INSERT INTO discovery_papers(remote_id,original_url,title,latest_version,oai_datestamp) VALUES('2609.00041','https://arxiv.org/abs/2609.00041','Existing target','v3',DATE '2026-09-28')"))
    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT raw_citation FROM reference_entries")) == raw
        assert conn.scalar(text("SELECT count(*) FROM reference_citation_links")) == 0
    assert reconcile_pending(eng, CitationPolicy()) == {"batches": 1, "references": 1}
    with eng.connect() as conn:
        row = conn.execute(text("SELECT match_status, arxiv_id, cited_version FROM reference_citation_links")).one()
        assert tuple(row) == ("linked", "2609.00041", "v2")
        with pytest.raises(IntegrityError):  # a target is required for, and only for, linked rows
            conn.execute(text("UPDATE reference_citation_links SET discovery_paper_id=NULL"))
        conn.rollback()
        with pytest.raises(IntegrityError):  # unlinked statuses may not carry an exact arXiv ID
            conn.execute(text("UPDATE reference_citation_links SET match_status='multiple_arxiv_ids',"
                              " discovery_paper_id=NULL"))
        conn.rollback()
    eng.dispose()


def test_0020_upgrades_m5b_database_with_data_and_binds_review_requests(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0019")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m6a@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M5b item')"
                                " RETURNING id"), {"o": owner})
        common = conn.scalar(text("INSERT INTO library_items(visibility,title) VALUES('common','Common') RETURNING id"))
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,version_id,"
                               "idempotency_key,state,rights_epoch_at_start) VALUES('upload','private',:o,:i,:v,"
                               "'m5b-existing','ready',0) RETURNING id"), {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,job_id,storage_key,verified_mime,byte_size,"
                                  "checksum_sha256) VALUES(:i,:v,:j,'existing-md','text/markdown',100,:h) RETURNING id"),
                             {"i": item, "v": version, "j": job, "h": "c" * 64})
    upgrade(empty_db_url)
    insert = text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,
        file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at)
        VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id""")
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT count(*) FROM library_items")) == 2
        assert conn.scalar(text("SELECT checksum_sha256 FROM source_files WHERE id=:f"), {"f": source}) == "c" * 64
        assert conn.scalar(text("SELECT count(*) FROM review_requests")) == 0
        with pytest.raises(IntegrityError):  # the checksum must be the file's own
            conn.execute(insert, {"i": item, "v": version, "f": source, "o": owner, "h": "d" * 64})
        conn.rollback()
        with pytest.raises(IntegrityError):  # a common item has no owner to request review
            conn.execute(insert, {"i": common, "v": version, "f": source, "o": owner, "h": "c" * 64})
        conn.rollback()
        assert conn.scalar(insert, {"i": item, "v": version, "f": source, "o": owner, "h": "c" * 64})
        conn.commit()
    eng.dispose()


def test_0021_upgrades_m6a_database_keeps_common_listed_and_requests(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0020")
    eng = create_engine(empty_db_url)
    verifier = hash_password("synthetic-password-long")
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m6b@example.org',:p) RETURNING id"),
                            {"p": verifier})
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('adm6b@example.org',:p,"
                                 "'admin') RETURNING id"), {"p": verifier})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M6a')"
                                " RETURNING id"), {"o": owner})
        common = conn.scalar(text("INSERT INTO library_items(visibility,title) VALUES('common','Legacy') RETURNING id"))
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                                  "checksum_sha256) VALUES(:i,:v,'m6a-key','text/markdown',100,:h) RETURNING id"),
                             {"i": item, "v": version, "h": "e" * 64})
        request = text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,
            file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at)
            VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id""")
        ids = {"i": item, "v": version, "f": source, "o": owner, "h": "e" * 64}
        withdrawn = conn.scalar(request, ids)
        conn.execute(text("UPDATE review_requests SET status='withdrawn', closed_at=now(), closed_by_user_id=:o,"
                          " close_reason_code='withdrawn_by_requester' WHERE id=:r"), {"o": owner, "r": withdrawn})
        open_rr = conn.scalar(request, ids)
    upgrade(empty_db_url, "0021")
    with eng.connect() as conn:
        states = dict(conn.execute(text("SELECT id, publication_state FROM library_items")).all())
        assert states == {item: None, common: "listed"}  # existing common items keep their visibility
        assert dict(conn.execute(text("SELECT id, status FROM review_requests")).all()) == {
            withdrawn: "withdrawn", open_rr: "open"}
    with eng.begin() as conn:  # the open M6a request can be approved into a pending copy
        pending = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title)"
                                   " VALUES('common','pending','Copy') RETURNING id"))
        pv = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": pending})
        pf = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                              "checksum_sha256) VALUES(:i,:v,'copy-key','text/markdown',100,:h) RETURNING id"),
                         {"i": pending, "v": pv, "h": "e" * 64})
        conn.execute(text("""INSERT INTO access_rights(item_id,version_id,source_file_id,file_checksum_sha256,
            origin_kind,origin_review_request_id,verified_source,evidence_note,checked_on,reviewer_user_id,recorded_at,
            scope_audience,permits_original_download,permits_fulltext_search,permits_external_processing,
            rights_epoch_at_grant) VALUES(:i,:v,:f,:h,'review_request',:r,'checked','evidence',CURRENT_DATE,:a,now(),
            'instance_users',false,false,false,0)"""),
                     {"i": pending, "v": pv, "f": pf, "h": "e" * 64, "r": open_rr, "a": admin})
        conn.execute(text("UPDATE review_requests SET status='approved', closed_at=now(), closed_by_user_id=:a,"
                          " close_reason_code='approved_by_admin' WHERE id=:r"), {"a": admin, "r": open_rr})
    with eng.connect() as conn:
        with pytest.raises(IntegrityError):  # nothing can publish the pending copy yet
            conn.execute(text("UPDATE library_items SET publication_state='listed' WHERE id=:i"), {"i": pending})
        conn.rollback()
    eng.dispose()
    with pytest.raises(Exception, match="approved review requests exist"):
        command.downgrade(alembic_config(empty_db_url), "0020")


def test_0022_upgrades_m6b1_database_keeps_pending_and_listed_then_gates_publication(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0021")
    eng = create_engine(empty_db_url)
    verifier = hash_password("synthetic-password-long")
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m6c@example.org',:p) RETURNING id"),
                            {"p": verifier})
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('adm6c@example.org',:p,"
                                 "'admin') RETURNING id"), {"p": verifier})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'P')"
                                " RETURNING id"), {"o": owner})
        legacy = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title)"
                                  " VALUES('common','listed','Legacy') RETURNING id"))
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                                  "checksum_sha256) VALUES(:i,:v,'private-key','text/markdown',100,:h) RETURNING id"),
                             {"i": item, "v": version, "h": "f" * 64})
        rr = conn.scalar(text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,
            file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at)
            VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id"""),
                         {"i": item, "v": version, "f": source, "o": owner, "h": "f" * 64})
        pending = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title)"
                                   " VALUES('common','pending','Copy') RETURNING id"))
        pv = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": pending})
        pf = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                              "checksum_sha256) VALUES(:i,:v,'copy-key','text/markdown',100,:h) RETURNING id"),
                         {"i": pending, "v": pv, "h": "f" * 64})
        rights = conn.scalar(text("""INSERT INTO access_rights(item_id,version_id,source_file_id,file_checksum_sha256,
            origin_kind,origin_review_request_id,verified_source,evidence_note,checked_on,reviewer_user_id,recorded_at,
            scope_audience,permits_original_download,permits_fulltext_search,permits_external_processing,
            rights_epoch_at_grant) VALUES(:i,:v,:f,:h,'review_request',:r,'checked','evidence',CURRENT_DATE,:a,now(),
            'instance_users',true,false,false,0) RETURNING id"""),
                             {"i": pending, "v": pv, "f": pf, "h": "f" * 64, "r": rr, "a": admin})
        conn.execute(text("UPDATE review_requests SET status='approved', closed_at=now(), closed_by_user_id=:a,"
                          " close_reason_code='approved_by_admin' WHERE id=:r"), {"a": admin, "r": rr})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        states = dict(conn.execute(text("SELECT id, publication_state FROM library_items")).all())
        assert states == {item: None, legacy: "listed", pending: "pending"}  # nothing changes state on upgrade
        assert conn.scalar(text("SELECT revoked_at FROM access_rights WHERE id=:r"), {"r": rights}) is None
        assert conn.scalar(text("SELECT count(*) FROM common_publications")) == 0
        assert conn.scalar(text("SELECT count(*) FROM deletion_tombstones")) == 0
    with pytest.raises(IntegrityError, match="publication record"), eng.begin() as conn:
        conn.execute(text("UPDATE library_items SET publication_state='listed' WHERE id=:i"), {"i": pending})
    with pytest.raises(IntegrityError, match="recorded rights"), eng.begin() as conn:
        conn.execute(text("UPDATE library_items SET publication_state='revoked', rights_epoch=1 WHERE id=:i"),
                     {"i": legacy})
    with eng.begin() as conn:  # the pre-existing pending copy can be published with its record
        conn.execute(text("UPDATE library_items SET publication_state='listed' WHERE id=:i"), {"i": pending})
        conn.execute(text("""INSERT INTO common_publications(item_id,access_rights_id,file_checksum_sha256,
            verified_byte_size,published_at,published_by_user_id) VALUES(:i,:r,:h,100,now(),:a)"""),
                     {"i": pending, "r": rights, "h": "f" * 64, "a": admin})
    eng.dispose()
    with pytest.raises(Exception, match="published or revoked common copies exist"):
        command.downgrade(alembic_config(empty_db_url), "0021")


def test_collection_membership_foreign_keys_restrict(migrated_db_url: str) -> None:
    eng = create_engine(migrated_db_url)
    with eng.connect() as conn:
        delete_actions = conn.scalars(text("""SELECT confdeltype FROM pg_constraint
            WHERE conrelid='collection_items'::regclass AND contype='f'""")).all()
    eng.dispose()
    assert delete_actions == ["r", "r"]


def test_0016_upgrades_m3c_database_with_data(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0015")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        user = conn.scalar(text("INSERT INTO users (email, password_verifier) VALUES ('m4a@example.org', :v) RETURNING id"),
                           {"v": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items (visibility, owner_user_id, title)"
                                " VALUES ('private', :u, 'Synthetic M3c item') RETURNING id"), {"u": user})
        collection = conn.scalar(text("INSERT INTO collections (owner_user_id, name)"
                                      " VALUES (:u, 'Existing') RETURNING id"), {"u": user})
        conn.execute(text("INSERT INTO collection_items (collection_id, item_id) VALUES (:c,:i)"),
                     {"c": collection, "i": item})
        usage = conn.scalar(text("INSERT INTO search_usage (user_id, day, task_type, model_id, provider,"
                                 " status, reserved_usd, cost_status) VALUES (:u, CURRENT_DATE, 'rerank',"
                                 " 'fixture', 'openrouter', 'uncertain', 0.01, 'unknown_reserved_ceiling') RETURNING id"),
                            {"u": user})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT title FROM library_items WHERE id=:i"), {"i": item}) == "Synthetic M3c item"
        assert conn.scalar(text("SELECT count(*) FROM collection_items WHERE collection_id=:c"), {"c": collection}) == 1
        assert conn.scalar(text("SELECT status FROM search_usage WHERE id=:u"), {"u": usage}) == "uncertain"
        conn.execute(text("INSERT INTO search_usage (user_id, day, task_type, model_id, provider,"
                          " status, reserved_usd, cost_status) VALUES (:u, CURRENT_DATE, 'generation',"
                          " 'openai/gpt-6-sol', 'openrouter', 'reserved', 0.01, 'unknown')"), {"u": user})
    eng.dispose()


def test_0015_upgrades_m3b_database_with_existing_data(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0014")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m3c@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M3b existing') RETURNING id"), {"o": owner})
        conn.execute(text("INSERT INTO search_request_days(day,user_id,request_count) VALUES(CURRENT_DATE,:o,7)"), {"o": owner})
    upgrade(empty_db_url)
    with eng.begin() as conn:
        assert conn.scalar(text("SELECT request_count FROM search_request_days WHERE user_id=:o"), {"o": owner}) == 7
        assert conn.scalar(text("SELECT title FROM library_items WHERE id=:i"), {"i": item}) == "M3b existing"
        collection = conn.scalar(text("INSERT INTO collections(owner_user_id,name) VALUES(:o,'Reading') RETURNING id"), {"o": owner})
        conn.execute(text("INSERT INTO collection_items(collection_id,item_id) VALUES(:c,:i)"), {"c": collection, "i": item})
        assert conn.scalar(text("SELECT count(*) FROM collection_items WHERE collection_id=:c"), {"c": collection}) == 1
    eng.dispose()


def test_0009_upgrades_database_with_m2b_pdf_data(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0008")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m3a@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M2b fixture') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id,coverage) VALUES(:i,'partial') RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,version_id,idempotency_key,state,rights_epoch_at_start,extraction_finished_at) VALUES('upload','private',:o,:i,:v,'m2b-existing','awaiting_index',0,now()) RETURNING id"),
                          {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,job_id,storage_key,verified_mime,byte_size,page_count,checksum_sha256) VALUES(:i,:v,:j,'existing-original','application/pdf',100,1,:h) RETURNING id"),
                             {"i": item, "v": version, "j": job, "h": "a" * 64})
        conn.execute(text("INSERT INTO pdf_pages(item_id,version_id,source_file_id,page_number,text,width,height,role,body_chars,body_page,qualifies,layout_mode,pipeline_version) VALUES(:i,:v,:f,1,'Introduction body',612,792,'body',16,true,true,'single','pypdf-6.19.0-layout-v2')"),
                     {"i": item, "v": version, "f": source})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT state FROM ingestion_jobs WHERE id=:j"), {"j": job}) == "awaiting_index"
        assert conn.scalar(text("SELECT count(*) FROM pdf_pages WHERE source_file_id=:f"), {"f": source}) == 1
        assert conn.scalar(text("SELECT content_status FROM library_items WHERE id=:i"), {"i": item}) == "metadata_only"
        assert conn.scalar(text("SELECT active_index_generation_id FROM library_items WHERE id=:i"), {"i": item}) is None
        assert conn.scalar(text("SELECT count(*) FROM index_generations")) == 0
    eng.dispose()


def test_0010_backfills_existing_embedding_calls_by_owner(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0009")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('owner@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'Synthetic') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,version_id,idempotency_key,state,rights_epoch_at_start) VALUES('upload','private',:o,:i,:v,'existing','awaiting_index',0) RETURNING id"),
                          {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,job_id,storage_key,verified_mime,byte_size,checksum_sha256) VALUES(:i,:v,:j,'existing','text/markdown',1,:h) RETURNING id"),
                             {"i": item, "v": version, "j": job, "h": "a" * 64})
        conn.execute(text("INSERT INTO embedding_calls(source_file_id,model_id,provider,pipeline_version,chunk_hash,status,reserved_usd,cost_status,reserved_day) VALUES(:f,'model','provider','pipeline',:h,'uncertain',0.00000100,'unknown',CURRENT_DATE)"),
                     {"f": source, "h": "a" * 64})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT owner_user_id FROM embedding_calls")) == owner
        assert conn.scalar(text("SELECT reserved_usd FROM embedding_user_budget_days WHERE user_id=:o"), {"o": owner}) == Decimal("0.00000100")
    eng.dispose()


def test_0011_upgrades_m3a_data_without_changing_it(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0010")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m3b-owner@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'M3a existing') RETURNING id"), {"o": owner})
        conn.execute(text("INSERT INTO embedding_budget_days(day,committed_usd,reserved_usd) VALUES(CURRENT_DATE,0.001,0.002)"))
        conn.execute(text("INSERT INTO embedding_user_budget_days(day,user_id,committed_usd,reserved_usd) VALUES(CURRENT_DATE,:o,0.001,0.002)"), {"o": owner})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT title FROM library_items WHERE id=:i"), {"i": item}) == "M3a existing"
        assert conn.scalar(text("SELECT committed_usd+reserved_usd FROM embedding_budget_days")) == Decimal("0.00300000")
        assert conn.scalar(text("SELECT committed_usd+reserved_usd FROM embedding_user_budget_days")) == Decimal("0.00300000")
        assert conn.scalar(text("SELECT count(*) FROM search_usage")) == 0
    eng.dispose()


def test_0012_backfills_existing_search_usage_warnings(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0011")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m3b-review@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        usage = conn.scalar(text("""INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,
            reserved_usd,cost_status) VALUES(:u,CURRENT_DATE,'rerank','model','openrouter','uncertain',
            0.00000100,'unknown_reserved_ceiling') RETURNING id"""), {"u": owner})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT status,reserved_usd,warnings FROM search_usage WHERE id=:i"), {"i": usage}).one()
    assert row.status == "uncertain" and row.reserved_usd == Decimal("0.00000100")
    assert row.warnings == []
    eng.dispose()


def test_0013_marks_prelease_reservations_uncertain_without_releasing_cost(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0012")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('lease@example.org',:p) RETURNING id"),
                            {"p": hash_password("synthetic-password-long")})
        usage = conn.scalar(text("""INSERT INTO search_usage(user_id,day,task_type,model_id,provider,status,
            reserved_usd,cost_status) VALUES(:u,CURRENT_DATE,'embed_query','model','openrouter','reserved',
            0.00000100,'unknown') RETURNING id"""), {"u": owner})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title)"
                                " VALUES('private',:u,'Lease fixture') RETURNING id"), {"u": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id,coverage)"
                                   " VALUES(:i,'complete') RETURNING id"), {"i": item})
        job = conn.scalar(text("""INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,
            item_id,version_id,idempotency_key,rights_epoch_at_start) VALUES
            ('upload','private',:u,:i,:v,'lease-fixture',0) RETURNING id"""),
                          {"u": owner, "i": item, "v": version})
        source = conn.scalar(text("""INSERT INTO source_files(item_id,version_id,job_id,storage_key,
            verified_mime,byte_size,checksum_sha256) VALUES
            (:i,:v,:j,'lease/original','text/markdown',1,:h) RETURNING id"""),
                             {"i": item, "v": version, "j": job, "h": "a" * 64})
        call = conn.scalar(text("""INSERT INTO embedding_calls(source_file_id,owner_user_id,model_id,
            provider,pipeline_version,chunk_hash,status,reserved_usd,cost_status,reserved_day)
            VALUES(:s,:u,'model','openrouter','pipeline',:h,'reserved',0.00000200,'unknown',CURRENT_DATE)
            RETURNING id"""), {"s": source, "u": owner, "h": "b" * 64})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT status,reserved_usd,cost_usd,cost_status,lease_until,"
                                " response_model_id,response_provider"
                                " FROM search_usage WHERE id=:i"), {"i": usage}).one()
    assert row == ("uncertain", Decimal("0.00000100"), None,
                   "lease_expired_reserved_ceiling", None, None, None)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT status,reserved_usd,cost_usd,cost_status,lease_until,"
                                " response_model_id,response_provider"
                                " FROM embedding_calls WHERE id=:i"), {"i": call}).one()
    assert row == ("uncertain", Decimal("0.00000200"), None,
                   "lease_expired_reserved_ceiling", None, None, None)
    eng.dispose()


def test_migration_sql_seeds_no_accounts() -> None:
    """Ledger backfill is allowed; migrations must never seed accounts."""
    out = io.StringIO()
    cfg = alembic_config("postgresql+psycopg://offline@localhost/offline", output=out)
    command.upgrade(cfg, "head", sql=True)
    inserts = [
        line for line in out.getvalue().splitlines()
        if line.strip().upper().startswith("INSERT INTO USERS")
    ]
    assert inserts == []


# --- M1b: 0001 -> 0002 on a database that already holds M1a data -------------


def _snapshot(conn) -> dict[str, list]:
    # Keep the 0001 projection stable as later migrations add nullable columns.
    columns = {"library_items": "id, visibility, owner_user_id, content_status, active_version_id, title, rights_epoch, created_by_user_id, created_at, updated_at"}
    return {
        t: [tuple(r) for r in conn.execute(text(f"SELECT {columns.get(t, '*')} FROM {t} ORDER BY 1")).all()]
        for t in ("users", "bootstrap_state", "library_items", "paper_versions", "audit_events")
    }


def test_upgrade_from_0001_keeps_existing_data(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0001")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        admin = conn.scalar(
            text(
                "INSERT INTO users (email, role, password_verifier)"
                " VALUES ('a@example.org', 'admin', :v) RETURNING id"
            ),
            {"v": hash_password("x" * 12)},
        )
        conn.execute(text("INSERT INTO bootstrap_state (admin_user_id) VALUES (:a)"), {"a": admin})
        item = conn.scalar(text("INSERT INTO library_items (visibility, title) VALUES ('common', 't') RETURNING id"))
        conn.execute(text("INSERT INTO paper_versions (item_id) VALUES (:i)"), {"i": item})
        conn.execute(text("INSERT INTO audit_events (actor_type, action, outcome) VALUES ('system', 'x', 'success')"))
    with eng.connect() as conn:
        before = _snapshot(conn)

    upgrade(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        after_first = _snapshot(conn)
    upgrade(empty_db_url)  # re-run: no-op
    with eng.connect() as conn:
        after_second = _snapshot(conn)
        for table in ("invitations", "sessions", "password_reset_tokens"):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    assert before == after_first == after_second

    # Downgrading only M1b keeps the M1a data too.
    command.downgrade(alembic_config(empty_db_url), "0001")
    with eng.connect() as conn:
        assert _snapshot(conn) == before
    assert "sessions" not in _tables(empty_db_url)
    eng.dispose()


def test_0005_preserves_received_m1c_job_and_original_metadata(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0004")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users (email, password_verifier) VALUES ('m2a@example.org', :v) RETURNING id"),
                            {"v": hash_password("test-password-long")})
        item = conn.scalar(text("INSERT INTO library_items (visibility, owner_user_id, title)"
                                " VALUES ('private', :o, 'User title') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions (item_id) VALUES (:i) RETURNING id"), {"i": item})
        job = conn.scalar(text("INSERT INTO ingestion_jobs (origin, actor_user_id, scope_visibility,"
                               " scope_owner_user_id, item_id, version_id, idempotency_key, rights_epoch_at_start)"
                               " VALUES ('upload', :o, 'private', :o, :i, :v, 'm1c-existing', 0) RETURNING id"),
                          {"o": owner, "i": item, "v": version})
        source = conn.scalar(text("INSERT INTO source_files (item_id, version_id, job_id, storage_key, verified_mime,"
                                  " byte_size, checksum_sha256) VALUES (:i, :v, :j, 'originals/old', 'text/markdown',"
                                  " 4, :h) RETURNING id"), {"i": item, "v": version, "j": job, "h": "a" * 64})
    upgrade(empty_db_url, "0005")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO metadata_candidates (item_id, version_id, source_file_id, field_name,"
                          " value, start_offset, end_offset, pipeline_version)"
                          " VALUES (:i, :v, :s, 'title', 'Test', 2, 6, 'markdown-lines-v1')"),
                     {"i": item, "v": version, "s": source})
        section = conn.scalar(text("INSERT INTO sections (item_id, version_id, source_file_id, kind, ordinal,"
                                   " text, start_offset, end_offset, start_line, end_line, pipeline_version)"
                                   " VALUES (:i, :v, :s, 'body', 0, '# Test', 0, 6, 1, 1, 'markdown-lines-v1') RETURNING id"),
                              {"i": item, "v": version, "s": source})
        conn.execute(text("INSERT INTO chunks (section_id, item_id, version_id, source_file_id, ordinal, text,"
                          " start_offset, end_offset, start_line, end_line, chunk_hash, pipeline_version)"
                          " VALUES (:section, :i, :v, :s, 0, '# Test', 0, 6, 1, 1, :h, 'markdown-lines-v1')"),
                     {"section": section, "i": item, "v": version, "s": source, "h": "b" * 64})
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT state, attempt_count, lease_token FROM ingestion_jobs WHERE id=:j"),
                           {"j": job}).one()
        assert row == ("received", 0, None)
        assert conn.scalar(text("SELECT id FROM source_files WHERE id=:s"), {"s": source}) == source
        assert conn.scalar(text("SELECT title FROM library_items WHERE id=:i"), {"i": item}) == "User title"
        assert conn.scalar(text("SELECT locator_kind FROM sections WHERE id=:s"), {"s": section}) == "markdown"
        assert conn.scalar(text("SELECT locator_kind FROM chunks WHERE section_id=:s"), {"s": section}) == "markdown"
        assert conn.scalar(text("SELECT locator_kind FROM metadata_candidates WHERE source_file_id=:s"),
                           {"s": source}) == "markdown"
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO chunks (section_id, item_id, version_id, source_file_id, ordinal, text,"
                              " locator_kind, start_offset, start_line, end_line, chunk_hash, pipeline_version)"
                              " VALUES (:section, :i, :v, :s, 1, 'bad', 'markdown', 0, 1, 1, :h, 'markdown-lines-v1')"),
                         {"section": section, "i": item, "v": version, "s": source, "h": "c" * 64})
    eng.dispose()


def test_0007_backfills_handoff_without_completing_job(empty_db_url: str) -> None:
    upgrade(empty_db_url, "0006")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        handoff = conn.scalar(text("INSERT INTO ingestion_jobs (origin, scope_visibility, idempotency_key,"
                                   " state, started_at, finished_at) VALUES ('discovery', 'common', 'handoff',"
                                   " 'awaiting_index', now() - interval '1 minute', now()) RETURNING id"))
        terminal = conn.scalar(text("INSERT INTO ingestion_jobs (origin, scope_visibility, idempotency_key,"
                                    " state, started_at, finished_at) VALUES ('discovery', 'common', 'terminal',"
                                    " 'terminal_failed', now() - interval '1 minute', now()) RETURNING id"))
    upgrade(empty_db_url)
    with eng.connect() as conn:
        row = conn.execute(text("SELECT extraction_finished_at, finished_at FROM ingestion_jobs WHERE id=:id"),
                           {"id": handoff}).one()
        assert row.extraction_finished_at is not None
        assert row.finished_at is None
        other = conn.execute(text("SELECT extraction_finished_at, finished_at FROM ingestion_jobs WHERE id=:id"),
                             {"id": terminal}).one()
        assert other.extraction_finished_at is None
        assert other.finished_at is not None
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.execute(text("UPDATE ingestion_jobs SET finished_at=now() WHERE id=:id"), {"id": handoff})
    eng.dispose()
