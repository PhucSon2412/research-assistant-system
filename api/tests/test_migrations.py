from __future__ import annotations

import uuid

import hashlib
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
    "auth_change_receipts", "auth_safety_heads", "auth_safety_state", "auth_hash_policies",
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
    "common_publications", "deletion_tombstones", "revocation_metadata_retentions",
    "recovery_registrations", "revocation_journal_records",
    # C2d-5e1 (0044)
    "object_delete_fences",
    # C2d-5e2 (0045)
    "orphan_object_completions",
    "orphan_object_fences",
    "orphan_reconcile_cursors",
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


def test_downgrade_to_base_and_upgrade_again(empty_db_url: str) -> None:
    # Not registered: once a database has a revocation journal, 0026 refuses the downgrade.
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), "base")
    assert _tables(empty_db_url) == {"alembic_version"}
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'ras\\_%'")) == 0
    eng.dispose()

    upgrade(empty_db_url)
    assert _tables(empty_db_url) == EXPECTED_TABLES


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
    upgrade(empty_db_url, "0032")
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

    # C1 adds its own fence; retain the historical fence assertions above.
    upgrade(empty_db_url)


def test_0023_removes_metadata_of_copies_already_purged_under_0022(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0022")
    eng = create_engine(empty_db_url)
    verifier = hash_password("synthetic-password-long")
    h = "d" * 64
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m6d@example.org',:p) RETURNING id"),
                            {"p": verifier})
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('adm6d@example.org',:p,"
                                 "'admin') RETURNING id"), {"p": verifier})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,"
                                "'Private title') RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id,source_url) VALUES(:i,'https://p.test/own')"
                                   " RETURNING id"), {"i": item})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                                  "checksum_sha256,original_filename) VALUES(:i,:v,'private-key','text/markdown',100,"
                                  ":h,'own.md') RETURNING id"), {"i": item, "v": version, "h": h})
        rr = conn.scalar(text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,status,
            file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,submitted_at)
            VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id"""),
                         {"i": item, "v": version, "f": source, "o": owner, "h": h})
        copy = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title)"
                                " VALUES('common','pending','Copy title') RETURNING id"))
        cv = conn.scalar(text("INSERT INTO paper_versions(item_id,source_url) VALUES(:i,'https://c.test/src')"
                              " RETURNING id"), {"i": copy})
        cf = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                              "checksum_sha256,original_filename) VALUES(:i,:v,'copy-key','text/markdown',100,:h,"
                              "'own.md') RETURNING id"), {"i": copy, "v": cv, "h": h})
        rights = conn.scalar(text("""INSERT INTO access_rights(item_id,version_id,source_file_id,file_checksum_sha256,
            origin_kind,origin_review_request_id,verified_source,verified_source_url,license_url,evidence_note,
            checked_on,reviewer_user_id,recorded_at,scope_audience,permits_original_download,permits_fulltext_search,
            permits_external_processing,scope_conditions,rights_epoch_at_grant) VALUES(:i,:v,:f,:h,'review_request',
            :r,'Admin source','https://c.test/src','https://l.test/x','Evidence note',CURRENT_DATE,:a,now(),
            'instance_users',true,false,false,'Conditions',0) RETURNING id"""),
                             {"i": copy, "v": cv, "f": cf, "h": h, "r": rr, "a": admin})
        conn.execute(text("UPDATE review_requests SET status='approved', closed_at=now(), closed_by_user_id=:a,"
                          " close_reason_code='approved_by_admin' WHERE id=:r"), {"a": admin, "r": rr})
    with eng.begin() as conn:  # revoked and purged under 0022, which left the metadata in place
        conn.execute(text("UPDATE library_items SET publication_state='revoked', rights_epoch=1 WHERE id=:i"),
                     {"i": copy})
        conn.execute(text("UPDATE access_rights SET revoked_at=now(), revoked_by_user_id=:a,"
                          " revocation_reason_code='other', rights_epoch_at_revoke=1 WHERE id=:r"),
                     {"a": admin, "r": rights})
        conn.execute(text("""INSERT INTO deletion_tombstones(target_kind,item_id,reason_code,prior_publication_state,
            rights_epoch,recorded_at,recorded_by_user_id,purge_due_at,purged_at,object_outcome)
            VALUES('common_copy',:i,'other','pending',1,now(),:a,now()+interval '1 day',now(),'deleted')"""),
                     {"i": copy, "a": admin})
    upgrade(empty_db_url, "0032")
    with eng.connect() as conn:
        left = conn.execute(text("""SELECT i.title, v.source_url, f.original_filename, r.verified_source,
            r.verified_source_url, r.license_url, r.evidence_note, r.scope_conditions
            FROM library_items i JOIN paper_versions v ON v.item_id=i.id JOIN source_files f ON f.item_id=i.id
            JOIN access_rights r ON r.item_id=i.id WHERE i.id=:i"""), {"i": copy}).one()
        assert set(left) == {None}
        assert conn.scalar(text("SELECT metadata_scrubbed_at IS NOT NULL FROM access_rights WHERE id=:r"),
                           {"r": rights})
        assert conn.scalar(text("SELECT cardinality(scrubbed_fields) FROM deletion_tombstones")) == 7
        # The private item, version and file keep everything.
        own = conn.execute(text("""SELECT i.title, v.source_url, f.original_filename FROM library_items i
            JOIN paper_versions v ON v.item_id=i.id JOIN source_files f ON f.item_id=i.id WHERE i.id=:i"""),
                           {"i": item}).one()
        assert tuple(own) == ("Private title", "https://p.test/own", "own.md")
    eng.dispose()
    with pytest.raises(Exception, match="removed metadata or retention records exist"):
        command.downgrade(alembic_config(empty_db_url), "0022")

    # C1 adds its own fence; retain the historical fence assertions above.
    upgrade(empty_db_url)


def _derived_fixture(conn, item, version, source, owner, job_origin: str, label: str) -> dict:
    """One row of every derived table for ``item`` (0023 schema), with ``label`` in each text."""
    p = {"i": item, "v": version, "f": source, "o": owner, "t": label, "origin": job_origin,
         "vis": "private" if owner else "common", "key": f"k-{label}", "h": hashlib.sha256(label.encode()).hexdigest()}
    p["job"] = conn.scalar(text("""INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,
        version_id,idempotency_key) VALUES(:origin,:vis,:o,:i,:v,:key) RETURNING id"""), p)
    p["s"] = conn.scalar(text("""INSERT INTO sections(item_id,version_id,source_file_id,kind,ordinal,text,locator_kind,
        start_offset,end_offset,start_line,end_line,pipeline_version) VALUES(:i,:v,:f,'body',0,:t,'markdown',0,5,1,1,
        'md-v1') RETURNING id"""), p)
    p["c"] = conn.scalar(text("""INSERT INTO chunks(item_id,version_id,source_file_id,section_id,ordinal,text,
        start_offset,end_offset,start_line,end_line,chunk_hash,pipeline_version,locator_kind)
        VALUES(:i,:v,:f,:s,0,:t,0,5,1,1,:h,'md-v1','markdown') RETURNING id"""), p)
    p["g"] = conn.scalar(text("""INSERT INTO index_generations(item_id,version_id,source_file_id,job_id,model_id,provider,
        dimensions,pipeline_version,index_version,expected_chunks,rights_epoch,state) VALUES(:i,:v,:f,:job,'m','openrouter',
        3,'md-v1','x',1,0,'active') RETURNING id"""), p)
    p["e"] = conn.scalar(text("""INSERT INTO embedding_calls(source_file_id,owner_user_id,model_id,provider,
        pipeline_version,chunk_hash,status,vector,dimensions,input_tokens,reserved_usd,cost_usd,cost_status,reserved_day)
        VALUES(:f,coalesce(:o,(SELECT id FROM users WHERE role='admin')),'m','openrouter','md-v1',:h,'completed',
        '[0.5,0.5,0.5]',3,5,0.001,0.0001,'usage_price_estimate',CURRENT_DATE) RETURNING id"""), p)
    conn.execute(text("""INSERT INTO index_entries(generation_id,chunk_id,embedding_call_id,chunk_hash,text)
        VALUES(:g,:c,:e,:h,:t)"""), p)
    conn.execute(text("""INSERT INTO pdf_pages(item_id,version_id,source_file_id,page_number,text,width,height,role,
        body_chars,body_page,qualifies,layout_mode,pipeline_version) VALUES(:i,:v,:f,1,:t,1,1,'body',5,true,true,
        'plain','pdf-v1')"""), p)
    p["r"] = conn.scalar(text("""INSERT INTO reference_entries(item_id,version_id,source_file_id,ordinal,raw_citation,
        fields,confidence,provenance,locator_kind,start_offset,end_offset,pipeline_version)
        VALUES(:i,:v,:f,0,:t,'{}','low','parser','markdown',0,5,'md-v1') RETURNING id"""), p)
    conn.execute(text("""INSERT INTO reference_citation_links(reference_id,matcher_version,raw_citation_sha256,
        match_status) VALUES(:r,'arxiv-exact-v2',:h,'no_arxiv_id')"""), p)
    conn.execute(text("""INSERT INTO metadata_candidates(item_id,version_id,source_file_id,field_name,value,locator_kind,
        start_offset,end_offset,pipeline_version) VALUES(:i,:v,:f,'title',:t,'markdown',0,5,'md-v1')"""), p)
    conn.execute(text("UPDATE library_items SET active_index_generation_id=:g WHERE id=:i"), p)
    return p


DERIVED_0024 = ("sections", "chunks", "pdf_pages", "reference_entries", "metadata_candidates", "index_generations")


def _derived_rows(conn, item) -> dict:
    out = {t: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {t} WHERE item_id=:i ORDER BY id"), {"i": item})]
           for t in DERIVED_0024}
    for name, sql in (("entries", "SELECT e.* FROM index_entries e JOIN chunks c ON c.id=e.chunk_id WHERE c.item_id=:i"),
                      ("links", "SELECT l.* FROM reference_citation_links l JOIN reference_entries r"
                                " ON r.id=l.reference_id WHERE r.item_id=:i"),
                      ("calls", "SELECT c.id, c.vector::text, c.chunk_hash, c.cost_usd, c.input_tokens, c.status"
                                " FROM embedding_calls c JOIN source_files f ON f.id=c.source_file_id WHERE f.item_id=:i"),
                      ("item", "SELECT active_index_generation_id FROM library_items WHERE id=:i")):
        out[name] = [tuple(r) for r in conn.execute(text(sql), {"i": item})]
    return out


def test_0024_removes_derived_rows_of_copies_purged_under_0023_and_fences_writes(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0023")
    eng = create_engine(empty_db_url)
    verifier = hash_password("synthetic-password-long")
    h = "e" * 64
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m6e@example.org',:p) RETURNING id"),
                            {"p": verifier})
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('adm6e@example.org',:p,"
                                 "'admin') RETURNING id"), {"p": verifier})
        item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,'P')"
                                " RETURNING id"), {"o": owner})
        version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
        source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                                  "checksum_sha256) VALUES(:i,:v,'private-key','text/markdown',100,:h) RETURNING id"),
                             {"i": item, "v": version, "h": h})
        private = _derived_fixture(conn, item, version, source, owner, "upload", "PRIVATETEXT")
        copies = {}
        for label in ("PURGEDTEXT", "PENDINGTEXT"):
            rr = conn.scalar(text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,
                status,file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,
                submitted_at) VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id"""),
                             {"i": item, "v": version, "f": source, "o": owner, "h": h})
            copy = conn.scalar(text("INSERT INTO library_items(visibility,publication_state) VALUES('common','pending')"
                                    " RETURNING id"))
            cv = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": copy})
            cf = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                                  "checksum_sha256) VALUES(:i,:v,:k,'text/markdown',100,:h) RETURNING id"),
                             {"i": copy, "v": cv, "h": h, "k": f"copy-{label}"})
            conn.execute(text("""INSERT INTO access_rights(item_id,version_id,source_file_id,file_checksum_sha256,
                origin_kind,origin_review_request_id,verified_source,evidence_note,checked_on,reviewer_user_id,
                recorded_at,scope_audience,permits_original_download,permits_fulltext_search,
                permits_external_processing,rights_epoch_at_grant) VALUES(:i,:v,:f,:h,'review_request',:r,'Admin src',
                'Evidence',CURRENT_DATE,:a,now(),'instance_users',true,true,false,0)"""),
                         {"i": copy, "v": cv, "f": cf, "h": h, "r": rr, "a": admin})
            conn.execute(text("UPDATE review_requests SET status='approved', closed_at=now(), closed_by_user_id=:a,"
                              " close_reason_code='approved_by_admin' WHERE id=:r"), {"a": admin, "r": rr})
            copies[label] = (copy, _derived_fixture(conn, copy, cv, cf, None, "promotion", label))
    for label, (copy, _) in copies.items():
        with eng.begin() as conn:
            conn.execute(text("UPDATE library_items SET publication_state='revoked', rights_epoch=1 WHERE id=:i"),
                         {"i": copy})
            conn.execute(text("""UPDATE access_rights SET revoked_at=now(), revoked_by_user_id=:a,
                revocation_reason_code='other', rights_epoch_at_revoke=1 WHERE item_id=:i"""), {"a": admin, "i": copy})
            conn.execute(text("""INSERT INTO deletion_tombstones(target_kind,item_id,reason_code,prior_publication_state,
                rights_epoch,recorded_at,recorded_by_user_id,purge_due_at) VALUES('common_copy',:i,'other','pending',1,
                now(),:a,now()+interval '1 day')"""), {"i": copy, "a": admin})
    purged = copies["PURGEDTEXT"][0]
    with eng.begin() as conn:  # purged under 0023 (metadata removed), which never looked at derived rows
        conn.execute(text("""UPDATE access_rights SET verified_source=NULL, evidence_note=NULL,
            metadata_scrubbed_at=now() WHERE item_id=:i"""), {"i": purged})
        conn.execute(text("""UPDATE deletion_tombstones SET purged_at=now(), object_outcome='deleted',
            scrubbed_fields='{}', retained_fields='{}' WHERE item_id=:i"""), {"i": purged})
        before = {"private": _derived_rows(conn, item), "pending": _derived_rows(conn, copies["PENDINGTEXT"][0])}
        cost_before = conn.execute(text("SELECT id, cost_usd, input_tokens, status FROM embedding_calls"
                                        " WHERE id=:e"), {"e": copies["PURGEDTEXT"][1]["e"]}).one()

    upgrade(empty_db_url, "0032")
    with eng.connect() as conn:
        after = _derived_rows(conn, purged)
        assert all(after[t] == [] for t in (*DERIVED_0024, "entries", "links"))
        assert after["item"] == [(None,)]
        call = conn.execute(text("SELECT id, cost_usd, input_tokens, status, vector, chunk_hash,"
                                 " content_removed_at IS NOT NULL FROM embedding_calls WHERE id=:e"),
                            {"e": copies["PURGEDTEXT"][1]["e"]}).one()
        assert tuple(call[:4]) == tuple(cost_before) and tuple(call[4:]) == (None, None, True)
        assert conn.scalar(text("SELECT ras_common_copy_derived_left(:i)"), {"i": purged}) == []
        # The private item and the copy still awaiting cleanup are left exactly as they were.
        assert _derived_rows(conn, item) == before["private"]
        assert _derived_rows(conn, copies["PENDINGTEXT"][0]) == before["pending"]
        dumped = " ".join(str(r) for t in ("sections", "chunks", "pdf_pages", "reference_entries", "index_entries",
                                             "metadata_candidates") for r in conn.execute(text(f"SELECT * FROM {t}")))
        assert "PURGEDTEXT" not in dumped and "PRIVATETEXT" in dumped and "PENDINGTEXT" in dumped
    with pytest.raises(IntegrityError, match="derived rows cannot be written for a revoked common copy"), \
            eng.begin() as conn:
        conn.execute(text("""INSERT INTO sections(item_id,version_id,source_file_id,kind,ordinal,text,locator_kind,
            start_offset,end_offset,start_line,end_line,pipeline_version) SELECT item_id,version_id,id,'body',1,'late',
            'markdown',0,4,1,1,'md-v1' FROM source_files WHERE item_id=:i"""), {"i": purged})
    with eng.begin() as conn:  # the private item's pipeline is not fenced
        conn.execute(text("UPDATE chunks SET text=text WHERE id=:c"), {"c": private["c"]})
    eng.dispose()
    with pytest.raises(Exception, match="removed embedding content exists"):
        command.downgrade(alembic_config(empty_db_url), "0023")

    # C1 adds its own fence; retain the historical fence assertions above.
    upgrade(empty_db_url)


def _reviewed_copy_0024(conn, owner, admin, *, h: str, label: str) -> tuple:
    """A private item/file, its approved request and the pending common copy with rights (0024 schema)."""
    item = conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:o,:t)"
                            " RETURNING id"), {"o": owner, "t": label})
    version = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": item})
    source = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                              "checksum_sha256) VALUES(:i,:v,:k,'text/markdown',100,:h) RETURNING id"),
                         {"i": item, "v": version, "h": h, "k": f"private-{label}"})
    rr = conn.scalar(text("""INSERT INTO review_requests(item_id,version_id,source_file_id,requester_user_id,
        status,file_checksum_sha256,rights_epoch_at_submit,notice_version,declared_source,declared_rights,
        submitted_at) VALUES(:i,:v,:f,:o,'open',:h,0,'n','src','rights',now()) RETURNING id"""),
                     {"i": item, "v": version, "f": source, "o": owner, "h": h})
    copy = conn.scalar(text("INSERT INTO library_items(visibility,publication_state) VALUES('common','pending')"
                            " RETURNING id"))
    cv = conn.scalar(text("INSERT INTO paper_versions(item_id) VALUES(:i) RETURNING id"), {"i": copy})
    cf = conn.scalar(text("INSERT INTO source_files(item_id,version_id,storage_key,verified_mime,byte_size,"
                          "checksum_sha256) VALUES(:i,:v,:k,'text/markdown',100,:h) RETURNING id"),
                     {"i": copy, "v": cv, "h": h, "k": f"copy-{label}"})
    conn.execute(text("""INSERT INTO access_rights(item_id,version_id,source_file_id,file_checksum_sha256,
        origin_kind,origin_review_request_id,verified_source,evidence_note,checked_on,reviewer_user_id,
        recorded_at,scope_audience,permits_original_download,permits_fulltext_search,
        permits_external_processing,rights_epoch_at_grant) VALUES(:i,:v,:f,:h,'review_request',:r,'Admin src',
        'Evidence',CURRENT_DATE,:a,now(),'instance_users',false,true,true,0)"""),
                 {"i": copy, "v": cv, "f": cf, "h": h, "r": rr, "a": admin})
    conn.execute(text("UPDATE review_requests SET status='approved', closed_at=now(), closed_by_user_id=:a,"
                      " close_reason_code='approved_by_admin' WHERE id=:r"), {"a": admin, "r": rr})
    return item, source, copy, cv, cf


def test_0025_pins_promotion_jobs_and_charges_common_calls_to_the_application(empty_db_url: str) -> None:
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0024")
    eng = create_engine(empty_db_url)
    verifier = hash_password("synthetic-password-long")
    with eng.begin() as conn:
        owner = conn.scalar(text("INSERT INTO users(email,password_verifier) VALUES('m6f@example.org',:p) RETURNING id"),
                            {"p": verifier})
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('adm6f@example.org',:p,"
                                 "'admin') RETURNING id"), {"p": verifier})
        item, source, copy, cv, cf = _reviewed_copy_0024(conn, owner, admin, h="f" * 64, label="M6F")
        # 0024 allowed a promotion job without its file (fixtures only) and a common call owned by the Admin.
        job = conn.scalar(text("""INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,version_id,
            idempotency_key,state) VALUES('promotion','common',:i,:v,'legacy-promotion','received') RETURNING id"""),
                          {"i": copy, "v": cv})
        upload = conn.scalar(text("""INSERT INTO ingestion_jobs(origin,scope_visibility,scope_owner_user_id,item_id,
            version_id,idempotency_key) VALUES('upload','private',:o,:i,(SELECT version_id FROM source_files
            WHERE id=:f),'upload-0024') RETURNING id"""), {"o": owner, "i": item, "f": source})
        legacy_call = conn.scalar(text("""INSERT INTO embedding_calls(source_file_id,owner_user_id,model_id,provider,
            pipeline_version,chunk_hash,status,reserved_usd,cost_status,reserved_day) VALUES(:f,:a,'m','openrouter',
            'md-v1',:h,'uncertain',0.00000100,'unknown',CURRENT_DATE) RETURNING id"""),
                                  {"f": cf, "a": admin, "h": "c" * 64})
    upgrade(empty_db_url, "0032")
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0032"
        assert tuple(conn.execute(text("SELECT source_file_id, version_id FROM ingestion_jobs WHERE id=:j"),
                                  {"j": job}).one()) == (cf, cv)
        assert conn.scalar(text("SELECT source_file_id FROM ingestion_jobs WHERE id=:j"), {"j": upload}) is None
        # Existing rows are not rewritten: the legacy call keeps its owner and cost.
        assert tuple(conn.execute(text("SELECT owner_user_id, reserved_usd FROM embedding_calls WHERE id=:c"),
                                  {"c": legacy_call}).one()) == (admin, Decimal("0.00000100"))
    insert_call = text("""INSERT INTO embedding_calls(source_file_id,owner_user_id,model_id,provider,pipeline_version,
        chunk_hash,status,reserved_usd,cost_status,reserved_day) VALUES(:f,:o,'m','openrouter','md-v1',:h,'reserved',
        0,'unknown',CURRENT_DATE)""")
    with pytest.raises(IntegrityError, match="charged to the application"), eng.begin() as conn:
        conn.execute(insert_call, {"f": cf, "o": admin, "h": "d" * 64})
    with pytest.raises(IntegrityError, match="keeps its pinned"), eng.begin() as conn:
        conn.execute(text("UPDATE ingestion_jobs SET source_file_id=NULL WHERE id=:j"), {"j": job})
    with eng.begin() as conn:
        conn.execute(insert_call, {"f": cf, "o": None, "h": "d" * 64})
    eng.dispose()
    # 0024 cannot represent an application-charged call.
    with pytest.raises(Exception, match="common copies were indexed"):
        command.downgrade(alembic_config(empty_db_url), "0024")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM embedding_calls WHERE owner_user_id IS NULL"))
    eng.dispose()
    command.downgrade(alembic_config(empty_db_url), "0024")
    upgrade(empty_db_url, "0032")
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:  # re-pinned on the way back up
        assert conn.scalar(text("SELECT source_file_id FROM ingestion_jobs WHERE id=:j"), {"j": job}) == cf
    eng.dispose()

    # C1 adds its own fence; retain the historical fence assertions above.
    upgrade(empty_db_url)


def test_0025_refuses_a_promotion_job_it_cannot_pin(empty_db_url: str) -> None:
    upgrade(empty_db_url, "0024")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:  # a legacy common item has no AccessRights, hence no reviewed file
        item = conn.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common',"
                                "'listed','Legacy') RETURNING id"))
        conn.execute(text("INSERT INTO ingestion_jobs(origin,scope_visibility,item_id,idempotency_key)"
                          " VALUES('promotion','common',:i,'orphan-promotion')"), {"i": item})
    eng.dispose()
    with pytest.raises(Exception, match="promotion jobs without a reviewed common file"):
        upgrade(empty_db_url)


def test_0026_leaves_registration_to_the_operator_and_imports_existing_tombstones(empty_db_url: str,
                                                                                   tmp_path) -> None:
    from types import SimpleNamespace

    from app import journal, recovery
    from app.clock import utcnow
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0025")
    eng = create_engine(empty_db_url)
    with eng.begin() as conn:
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('a@x.test',:p,'admin')"
                                 " RETURNING id"), {"p": hash_password("m6c1-migration-password")})
        # A tombstone written before M6c-1 (its copy is not in this database, as after a restore).
        tomb = conn.scalar(text("""INSERT INTO deletion_tombstones(target_kind,item_id,reason_code,
            prior_publication_state,rights_epoch,recorded_at,recorded_by_user_id,purge_due_at,storage_key)
            VALUES('common_copy',gen_random_uuid(),'rights_withdrawn','listed',1,now(),:a,now()+interval '1 day',
            'originals/0123456789abcdef0123456789abcdef') RETURNING id"""), {"a": admin})
    upgrade(empty_db_url, "0032")
    command.downgrade(alembic_config(empty_db_url), "0025")  # nothing registered yet: allowed
    upgrade(empty_db_url)
    settings = SimpleNamespace(recovery=SimpleNamespace(journal_path=tmp_path / "revocations.jsonl",
                                                        gate_lock_timeout_ms=2000))
    assert recovery.evaluate(settings, eng).reason == "journal_missing"  # the migration registers nothing
    with pytest.raises(recovery.RecoveryError, match="database_not_new"):
        recovery.init_new(eng, settings, utcnow())
    _, imported = recovery.register_existing(eng, settings, utcnow())
    assert imported == 1 and recovery.evaluate_integrity(settings, eng).reason == "open"
    (genesis, record) = journal.read(settings.recovery.journal_path).records
    assert genesis.body["mode"] == "existing_db" and record.body["id"] == str(tomb)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT tombstone_id FROM revocation_journal_records WHERE seq=2")) == tomb
    eng.dispose()
    with pytest.raises(Exception, match="auth safety history prevents downgrade"):
        command.downgrade(alembic_config(empty_db_url), "0025")


def test_0027_backfills_revoke_records_unconfirmed_so_recover_must_confirm_them(empty_db_url: str,
                                                                                tmp_path) -> None:
    from types import SimpleNamespace

    from app import journal, recovery
    from app.clock import utcnow
    from app.identity.passwords import hash_password

    upgrade(empty_db_url, "0026")
    eng = create_engine(empty_db_url)
    path = tmp_path / "revocations.jsonl"
    genesis = journal.genesis(uuid.uuid4(), utcnow(), "new_install")
    with eng.begin() as conn:
        admin = conn.scalar(text("INSERT INTO users(email,password_verifier,role) VALUES('a@x.test',:p,'admin')"
                                 " RETURNING id"), {"p": hash_password("m6c1-migration-password")})
        body = {"id": str(uuid.uuid4()), "target_kind": "common_copy", "item_id": str(uuid.uuid4()),
                "version_id": None, "source_file_id": None, "storage_key": None, "access_rights_id": None,
                "reason_code": "other", "prior_publication_state": "listed", "rights_epoch": 1,
                "recorded_at": utcnow().isoformat(), "recorded_by_user_id": str(admin),
                "purge_due_at": utcnow().isoformat(), "retentions": []}
        revocation = journal.next_record(genesis, journal.REVOCATION, body)
        conn.execute(text("INSERT INTO recovery_registrations(id,journal_id,genesis_hash,mode,registered_at)"
                          " VALUES(1,:j,:h,'new_install',now())"), {"j": genesis.body["journal_id"], "h": genesis.hash})
        for record, via, tomb in ((genesis, "genesis", None), (revocation, "revoke", body["id"])):
            conn.execute(text("""INSERT INTO revocation_journal_records(seq,kind,record_hash,line,tombstone_id,
                applied_via,result) VALUES(:s,:k,:h,:l,:t,:v,'x')"""),
                         {"s": record.seq, "k": record.kind, "h": record.hash, "l": record.line, "t": tomb, "v": via})
        conn.execute(text("""INSERT INTO deletion_tombstones(id,target_kind,item_id,reason_code,prior_publication_state,
            rights_epoch,recorded_at,recorded_by_user_id,purge_due_at)
            VALUES(:t,'common_copy',:i,'other','listed',1,now(),:a,now()+interval '1 day')"""),
                     {"t": body["id"], "i": body["item_id"], "a": admin})
    journal.create(path, [genesis, revocation])
    upgrade(empty_db_url, "0032")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT seq, journal_confirmed FROM revocation_journal_records ORDER BY seq")).all() \
            == [(1, True), (2, False)]
    settings = SimpleNamespace(recovery=SimpleNamespace(journal_path=path, gate_lock_timeout_ms=2000,
                                                        cleanup_passes=1),
                               retention=SimpleNamespace(cleanup_batch_size=10))
    # A 0026 revocation may have been the complete-line-but-fsync-failed case: closed until recover confirms.
    assert recovery.evaluate(settings, eng).reason == "journal_unconfirmed"
    with pytest.raises(Exception, match="registered with a revocation journal"):
        command.downgrade(alembic_config(empty_db_url), "0026")
    upgrade(empty_db_url)
    report = recovery.recover(eng, None, settings, utcnow())
    assert report.journal_confirmed == 1 and report.reconciliation_complete
    assert recovery.evaluate_integrity(settings, eng).reason == "open"
    assert report.gate == "auth_safety_pending"
    # Confirmed or not, a registered database never goes below 0027: 0026 code would open the gate again
    # after the next revocation whose fsync fails (PR #26 review).
    with pytest.raises(Exception, match="auth safety history prevents downgrade"):
        command.downgrade(alembic_config(empty_db_url), "0026")
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        assert conn.scalar(text("SELECT count(*) FROM revocation_journal_records WHERE NOT journal_confirmed")) == 0
    eng.dispose()


def test_0027_downgrades_only_a_database_never_registered(empty_db_url: str) -> None:
    upgrade(empty_db_url)
    command.downgrade(alembic_config(empty_db_url), "0026")  # no registration, no journal record
    eng = create_engine(empty_db_url)
    with eng.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0026"
        assert "journal_confirmed" not in {c["name"] for c in inspect(conn).get_columns("revocation_journal_records")}
    eng.dispose()
    upgrade(empty_db_url)


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


def test_migration_sql_seeds_no_accounts(empty_db_url) -> None:
    """Execute the offline artifact: a routine body is not an executed seed.

    Checking rows also detects seeds in DO blocks or function calls, which the
    old line-prefix scan missed. Ledger/policy rows may still be initialized.
    """
    out = io.StringIO()
    cfg = alembic_config("postgresql+psycopg://offline@localhost/offline", output=out)
    command.upgrade(cfg, "head", sql=True)
    engine = create_engine(empty_db_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with engine.connect() as conn:
            # No driver parameter mapping: PL/pgSQL format('%I',...) and LIKE
            # patterns in a complete offline script are literal SQL here.
            with conn.connection.driver_connection.cursor() as cursor:
                cursor.execute(out.getvalue())
            assert conn.scalar(text("SELECT count(*) FROM users")) == 0
            assert conn.scalar(text("SELECT count(*) FROM bootstrap_state")) == 0
    finally:
        engine.dispose()


# --- M1b: 0001 -> 0002 on a database that already holds M1a data -------------


def _snapshot(conn) -> dict[str, list]:
    # Keep the 0001 projection stable as later migrations add nullable columns.
    columns = {"bootstrap_state": "id, admin_user_id, completed_at", "users": "id, email, display_name, role, status, password_verifier, password_changed_at, created_at, updated_at", "library_items": "id, visibility, owner_user_id, content_status, active_version_id, title, rights_epoch, created_by_user_id, created_at, updated_at"}
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

    # Historical M1b downgrade, before installing the C1 baseline.
    upgrade(empty_db_url, "0002")
    command.downgrade(alembic_config(empty_db_url), "0001")
    with eng.connect() as conn:
        assert _snapshot(conn) == before
    assert "sessions" not in _tables(empty_db_url)
    eng.dispose()

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
