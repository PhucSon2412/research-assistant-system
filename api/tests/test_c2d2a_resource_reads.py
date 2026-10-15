"""Thirteen HTTP reads: runtime LOGIN, real SQL receipts/gates/ACL and bounded races."""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import auth_safety, citations, publication, recovery
from app.api import resource_reads
from app.api.errors import ApiError
from app.api.routes import library, collections, discovery
from app.main import app
from app.models import Collection, CollectionItem, DiscoverySource, LibraryItem, UserSession
from app.models.enums import RevocationReason
from tests.test_c2d1_final_caller import browser, mutate, wait_for, close
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import operational_world as world, issue, accept, PASSWORD, EMAIL
from tests.test_auth_safety_foundation import NOW
from tests.resource_fixtures import source, derived, common_copy
from tests.test_citations import oai_page
from app.discovery import _write_page, parse_page

ROUTES = {
    "list": (library, "list_items", "/library/items"),
    "count": (library, "count_items", "/library/items/count"),
    "detail": (library, "get_item", "/library/items/{item}"),
    **{key: (library, "get_"+key.replace("-", "_"), "/library/items/{item}/"+key)
       for key in ("chunks", "metadata-candidates", "coverage", "references")},
    "collections": (collections, "list_collections", "/collections"),
    "members": (collections, "list_collection_items", "/collections/{collection}/items"),
    "topics": (discovery, "list_topics", "/discovery/topics"),
    "papers": (discovery, "list_papers", "/discovery/papers"),
    "citing": (discovery, "citing_items", "/discovery/papers/{paper}/citing-items"),
    "sources": (discovery, "source_status", "/discovery/sources"),
}


@pytest.fixture
def resources(browser):
    b = browser
    b.ids = source(b.w, b.w.uid, "OWN-PRIVATE")
    with Session(b.w.engine) as db:
        c = Collection(owner_user_id=b.w.uid, name="Owned")
        db.add(c); db.flush(); cid=c.id
        db.add(CollectionItem(collection_id=cid, item_id=b.ids.item))
        db.add(DiscoverySource(topic="cs.AI", window_from=NOW.date(), resumption_token="PRIVATE-TOKEN"))
        db.flush()
        _write_page(db, "cs.AI", parse_page(oai_page([("2609.00011", "Metadata target", 1, "2026-09-28")]), 100000), NOW)
        db.commit()
        pid = db.scalar(text("SELECT id FROM discovery_papers"))
    b.path = lambda key: ROUTES[key][2].format(item=b.ids.item, collection=cid, paper=pid)
    b.collection, b.paper = cid, pid
    return b


@pytest.mark.parametrize("key", ROUTES)
def test_all_thirteen_success_and_empty_branches_final_boundary(resources, monkeypatch, key, caplog):
    b=resources; calls=[]; real=resource_reads.final_caller
    def checked(db, **kw):
        proof=real(db,**kw); calls.append((db.get_transaction(),proof)); return proof
    monkeypatch.setattr(resource_reads,"final_caller",checked)
    before=b.w.path.read_bytes()
    response=b.client.get(b.path(key))
    assert response.status_code==200,response.text
    assert len(calls)==2 and calls[0][0] is calls[1][0]
    assert all(p.session_id==b.w.actor.session_id and p.user_id==b.w.uid for _,p in calls)
    assert b.w.path.read_bytes()==before
    assert "PRIVATE-TOKEN" not in response.text+caplog.text
    with Session(b.w.runtime) as db:
        assert db.get(UserSession,b.w.actor.session_id).last_activity_at==NOW
    assert b.w.runtime.pool.checkedout()==0


@pytest.mark.parametrize("key", ROUTES)
@pytest.mark.parametrize("change", ["revoke","disable","generation","quarantine","held",
    "receipt_credential","receipt_status","wrong_receipt","head","latest_credential",
    "latest_status","hold","ahead","unconfirmed"])
def test_auth_changes_after_real_dependency(resources,key,change):
    b=resources
    b.after_dependency=lambda db,ctx: mutate(b,change)
    response=b.client.get(b.path(key))
    expected=503 if change in ("hold","ahead","unconfirmed","receipt_credential","receipt_status","wrong_receipt","head","latest_credential","latest_status") else 401
    assert response.status_code==expected,response.text
    assert response.json()=={"error":"service_recovering" if expected==503 else "session_expired"}
    if expected==401: assert "Max-Age=0" in response.headers["set-cookie"]
    assert "OWN-PRIVATE" not in response.text and "Metadata target" not in response.text


@pytest.mark.parametrize("key", ROUTES)
@pytest.mark.parametrize("kind,seconds,expected", [("idle",1799,200),("idle",1800,401),
                                                  ("absolute",28799,200),("absolute",28800,401)])
def test_clock_after_last_resource_query(resources,monkeypatch,key,kind,seconds,expected):
    b=resources; module,name,_=ROUTES[key]; real=getattr(module,"_read_"+name); calls=[]
    def read(*args,**kw):
        value=real(*args,**kw); calls.append(1)
        if len(calls)==3:
            b.at=NOW+timedelta(seconds=seconds)
        return value
    if kind=="absolute":
        def activity(db,ctx):
            with b.w.engine.begin() as conn:
                conn.execute(text("UPDATE sessions SET last_activity_at=:at WHERE id=:sid"),
                    {"at":NOW+timedelta(hours=7,minutes=59),"sid":ctx.session_id})
        b.after_dependency=activity
    monkeypatch.setattr(module,"_read_"+name,read)
    response=b.client.get(b.path(key))
    assert response.status_code==expected,response.text
    assert len(calls)==3


@pytest.mark.parametrize("key", ["list","detail","sources"])
def test_current_role_rebuilds_flags_and_sources(resources,key):
    b=resources
    common=common_copy(b.w,indexed=False)
    b.after_dependency=lambda db,ctx: mutate(b,"role")
    path=b.path(key) if key!="detail" else f"/library/items/{common.item}"
    response=b.client.get(path)
    if key=="sources": assert response.status_code==403 and response.json()=={"error":"forbidden"}
    else:
        assert response.status_code==200,response.text
        rows=response.json()["items"] if key=="list" else [response.json()["item"]]
        assert all(not row["can_edit"] for row in rows if row["visibility"]=="common")


@pytest.mark.parametrize("key", ROUTES)
def test_real_pending_all_thirteen(installation,key):
    with Session(installation.runtime) as db:
        assert auth_safety.cutover(db)=="auth_safety_pending"
    with TestClient(app) as client:
        path=ROUTES[key][2].format(item=uuid.uuid4(),collection=uuid.uuid4(),paper=uuid.uuid4())
        assert client.get(path).status_code==503
        assert client.get("/healthz").status_code==200
        assert client.get("/readyz").status_code==503


@pytest.mark.parametrize("download,search,provider,indexed,pending", [
    (False,True,False,True,False),(True,True,True,True,False),
    (True,False,False,False,False),(False,True,False,False,False),
    (True,True,True,False,True)])
def test_common_metadata_flags_original_and_derived_tuple(resources,download,search,provider,indexed,pending):
    b=resources; c=common_copy(b.w,download=download,search=search,provider=provider,indexed=indexed,pending=pending)
    response=b.client.get(f"/library/items/{c.item}")
    if pending:
        assert response.status_code==404
    else:
        assert response.status_code==200,response.text
        item=response.json()["item"]
        assert item["fulltext_search_available"]==bool(indexed and search)
        assert item["rag_available"]==bool(indexed and search and provider)
        assert bool(item["files"])==download
        assert item["jobs"]==[]
        assert item["versions"][0]["coverage"] is None or (indexed and search)
    # A second version/file on the same common item must not yield its text.
    with Session(b.w.engine) as db:
        from app.models import PaperVersion, SourceFile
        v=PaperVersion(item_id=c.item,version_label="other"); db.add(v); db.flush()
        f=SourceFile(item_id=c.item,version_id=v.id,storage_key=f"synthetic/{v.id}",byte_size=len(b"other"),
                     checksum_sha256="f"*64,verified_mime="text/markdown")
        db.add(f); db.flush(); other=SimpleNamespace(item=c.item,version=v.id,file=f.id); db.commit()
    derived(b.w,other,"WRONG-VERSION-SECRET")
    for suffix in ("chunks","coverage","references","metadata-candidates"):
        r=b.client.get(f"/library/items/{c.item}/{suffix}")
        assert r.status_code==(200 if indexed and search and not pending else 404),r.text
        assert "WRONG-VERSION-SECRET" not in r.text
        if suffix=="chunks" and r.status_code==200: assert r.json()["chunks"][0]["text"]=="COMMON-TEXT"


@pytest.mark.parametrize("corruption", ["epoch","generation","file","version","tombstone"])
def test_common_invalid_tuple_closes_text(resources,corruption):
    b=resources; c=common_copy(b.w)
    with b.w.engine.begin() as conn:
        if corruption=="tombstone":
            # Actual journaled revoke; never create an unjournaled tombstone.
            pass
        else:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            sql={"epoch":"UPDATE index_generations SET rights_epoch=1 WHERE item_id=:i",
                 "generation":"UPDATE index_generations SET state='superseded' WHERE item_id=:i",
                 "file":"UPDATE ingestion_jobs SET source_file_id=gen_random_uuid() WHERE item_id=:i",
                 "version":"UPDATE index_generations SET version_id=gen_random_uuid() WHERE item_id=:i"}[corruption]
            conn.execute(text(sql),{"i":c.item})
    if corruption=="tombstone": revoke_common(b,c)
    for suffix in ("chunks","coverage","references","metadata-candidates"):
        assert b.client.get(f"/library/items/{c.item}/{suffix}").status_code==404


def revoke_common(b,c):
    with Session(b.w.engine) as db:
        db.execute(text("SET LOCAL application_name='c2d2a_resource_writer'"))
        publication.revoke(db,c.admin,c.item,RevocationReason.RIGHTS_WITHDRAWN,NOW,timedelta(hours=24))


def wait_for_resource_writer(w, event):
    deadline=time.monotonic()+10
    with w.engine.connect() as observer:
        while time.monotonic()<deadline:
            found=observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database()"
                " AND application_name='c2d2a_resource_writer' AND wait_event=:event)"),{"event":event})
            observer.rollback()
            if found:return
            threading.Event().wait(.005)
    raise AssertionError("resource writer wait not observed")


@pytest.mark.parametrize("key", ["list","count","detail","chunks","coverage","references","metadata-candidates"])
def test_common_revoke_between_candidate_and_final(resources,monkeypatch,key):
    b=resources; c=common_copy(b.w); module,name,_=ROUTES[key]; real=getattr(module,"_read_"+name); calls=[]
    def read(*args,**kw):
        result=real(*args,**kw)
        if not calls: calls.append(1); revoke_common(b,c)
        return result
    monkeypatch.setattr(module,"_read_"+name,read)
    path=f"/library/items/{c.item}"+("/"+key if key not in ("list","count","detail") else "")
    if key in ("list","count"):path=b.path(key)+"?scope=common&limit=1"
    r=b.client.get(path)
    assert r.status_code==(200 if key in ("list","count") else 404),r.text
    if key in ("list","count"): assert r.json()["total"]==0
    assert "COMMON-TEXT" not in r.text and "Common readable" not in r.text


@pytest.mark.parametrize("change", ["delete_collection","membership","tombstone","stale_link"])
def test_collection_and_citation_changes_before_final(resources,monkeypatch,change):
    b=resources
    rid=derived(b.w,b.ids,"OWNER-TEXT")
    with Session(b.w.engine) as db:
        citations.reconcile_references(db,reference_ids=[rid],now=NOW); db.commit()
    key="members" if change in ("delete_collection","membership") else "citing"
    module,name,_=ROUTES[key]; real=getattr(module,"_read_"+name); once=[]
    def read(*args,**kw):
        value=real(*args,**kw)
        if not once:
            once.append(1)
            with b.w.engine.begin() as conn:
                if change in ("delete_collection","membership"):
                    conn.execute(text("DELETE FROM collection_items WHERE collection_id=:c"),{"c":b.collection})
                    if change=="delete_collection":conn.execute(text("DELETE FROM collections WHERE id=:c"),{"c":b.collection})
                elif change=="stale_link":conn.execute(text("UPDATE reference_entries SET raw_citation='changed' WHERE id=:r"),{"r":rid})
                else:conn.execute(text("INSERT INTO discovery_deleted_records(source,remote_id,oai_datestamp) VALUES('arxiv','2609.00011','2026-10-05')"))
        return value
    monkeypatch.setattr(module,"_read_"+name,read)
    r=b.client.get(b.path(key))
    assert r.status_code==(404 if change in ("delete_collection","tombstone") else 200),r.text
    if r.status_code==200:assert r.json()["total"]==0 and r.json()["items"]==[]


def test_count_page_churn_bounded_and_controlled(resources,monkeypatch):
    b=resources; real=library._read_count_items; calls=[]
    def read(*args,**kw):
        result=real(*args,**kw); calls.append(1)
        with b.w.engine.begin() as conn:
            conn.execute(text("INSERT INTO library_items(visibility,owner_user_id,title) VALUES('private',:u,'new')"),{"u":b.w.uid})
        return result
    monkeypatch.setattr(library,"_read_count_items",read)
    response=b.client.get(b.path("count"))
    assert response.status_code==409 and response.json()=={"error":"resource_scope_changed"}
    assert len(calls)==6 and b.w.runtime.pool.checkedout()==0


@pytest.mark.parametrize("winner", ["reader","writer"])
def test_actual_common_revoke_lock_race(resources,monkeypatch,winner):
    b=resources; c=common_copy(b.w); entered=threading.Event(); release=threading.Event()
    if winner=="reader":
        real=resource_reads.final_caller
        def pause(db,**kw):
            proof=real(db,**kw)
            if not entered.is_set():entered.set(); assert release.wait(15)
            return proof
        monkeypatch.setattr(resource_reads,"final_caller",pause)
    else:
        real=library._read_get_chunks
        def pause(*args,**kw):
            result=real(*args,**kw)
            if not entered.is_set():entered.set(); assert release.wait(15)
            return result
        monkeypatch.setattr(library,"_read_get_chunks",pause)
    with ThreadPoolExecutor(2) as pool:
        reader=pool.submit(b.client.get,f"/library/items/{c.item}/chunks")
        assert entered.wait(15)
        writer=pool.submit(revoke_common,b,c)
        try:
            if winner=="reader":wait_for_resource_writer(b.w,"transactionid"); assert not writer.done()
            else:writer.result(15)
        finally:release.set()
        response=reader.result(20); writer.result(20)
    assert response.status_code==(200 if winner=="reader" else 404),response.text
    assert b.client.get(f"/library/items/{c.item}/chunks").status_code==404
    assert b.w.runtime.pool.checkedout()==0


def test_reader_holds_no_auth_rows_and_auth_writer_waits_journal(resources,monkeypatch):
    b=resources; entered=threading.Event(); release=threading.Event(); real=resource_reads.final_caller
    def pause(db,**kw):
        proof=real(db,**kw)
        if not entered.is_set():entered.set(); assert release.wait(15)
        return proof
    monkeypatch.setattr(resource_reads,"final_caller",pause)
    with ThreadPoolExecutor(2) as pool:
        reader=pool.submit(b.client.get,b.path("members")); assert entered.wait(15)
        writer=pool.submit(close,b.w)
        try:wait_for(b.w,"advisory"); assert not writer.done()
        finally:release.set()
        assert reader.result(20).status_code==200; writer.result(20)
    assert b.client.get(b.path("members")).status_code==401
    assert b.w.runtime.pool.checkedout()==0


@pytest.mark.parametrize("key",["list","members","citing"])
def test_exact_caller_binding_after_dependency(resources,key):
    b=resources; other=accept(b.w,issue(b.w))
    def rebind(db,ctx):
        with b.w.engine.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            conn.execute(text("UPDATE sessions SET user_id=:u,credential_generation=:g WHERE id=:s"),
                         {"u":other.id,"g":other.credential_generation,"s":ctx.session_id})
    b.after_dependency=rebind
    r=b.client.get(b.path(key))
    assert r.status_code==401 and r.json()=={"error":"session_expired"}


@pytest.mark.parametrize("key",["list","sources"])
def test_current_role_after_last_query(resources,monkeypatch,key):
    b=resources; common=common_copy(b.w,indexed=False)
    module,name,_=ROUTES[key]; real=getattr(module,"_read_"+name); calls=[]
    def read(*args,**kw):
        data=real(*args,**kw);calls.append(1)
        if len(calls)==3:mutate(b,"role")
        return data
    monkeypatch.setattr(module,"_read_"+name,read)
    r=b.client.get(b.path(key))
    if key=="sources":assert r.status_code==403
    else:
        assert r.status_code==200,r.text
        assert len(calls)==6
        assert not next(i for i in r.json()["items"] if i["id"]==str(common.item))["can_edit"]


@pytest.mark.parametrize("key",["list","count","members","papers"])
def test_empty_count_page_success_still_checks_after_sql(resources,monkeypatch,key):
    b=resources; real=resource_reads.final_caller; calls=[]
    def check(db,**kw):calls.append(1);return real(db,**kw)
    monkeypatch.setattr(resource_reads,"final_caller",check)
    if key=="members":
        with b.w.engine.begin() as conn:
            conn.execute(text("DELETE FROM collection_items WHERE collection_id=:c"),{"c":b.collection})
    path=b.path(key)+("?scope=common" if key in ("list","count") else "?topic=unknown" if key=="papers" else "")
    r=b.client.get(path);assert r.status_code==200,r.text
    assert r.json()["total"]==0 and len(calls)==2
    if key!="count":assert r.json()["items"]==[]


def test_clock_after_observed_item_wait(resources):
    b=resources; entered=threading.Event()
    def pause(db,ctx):entered.set()
    b.after_dependency=pause
    with b.w.engine.connect() as blocker,ThreadPoolExecutor(1) as pool:
        blocker.execute(text("SELECT id FROM library_items WHERE id=:i FOR UPDATE"),{"i":b.ids.item})
        reader=pool.submit(b.client.get,b.path("detail"))
        try:
            assert entered.wait(15);wait_for(b.w,"transactionid")
            b.at=NOW+timedelta(minutes=30)
        finally:blocker.rollback()
        r=reader.result(20)
    assert r.status_code==401 and "OWN-PRIVATE" not in r.text


@pytest.mark.parametrize("winner",["reader","writer"])
def test_membership_parent_lock_both_orders(resources,monkeypatch,winner):
    b=resources;entered=threading.Event();release=threading.Event()
    real=resource_reads.final_caller
    def pause(db,**kw):
        proof=real(db,**kw)
        if not entered.is_set():entered.set();assert release.wait(15)
        return proof
    def remove():
        with b.w.engine.begin() as conn:
            conn.execute(text("SET LOCAL application_name='c2d2a_resource_writer'"))
            conn.execute(text("SELECT id FROM collections WHERE id=:c FOR UPDATE"),{"c":b.collection})
            conn.execute(text("DELETE FROM collection_items WHERE collection_id=:c"),{"c":b.collection})
    with ThreadPoolExecutor(2) as pool:
        if winner=="reader":
            monkeypatch.setattr(resource_reads,"final_caller",pause)
            reader=pool.submit(b.client.get,b.path("members"));assert entered.wait(15)
            writer=pool.submit(remove)
            try:wait_for_resource_writer(b.w,"transactionid");assert not writer.done()
            finally:release.set()
            r=reader.result(20);writer.result(20)
            assert r.status_code==200 and r.json()["total"]==1
        else:
            with b.w.engine.connect() as blocker:
                blocker.execute(text("SELECT id FROM collections WHERE id=:c FOR UPDATE"),{"c":b.collection})
                b.after_dependency=lambda db,ctx: entered.set()
                reader=pool.submit(b.client.get,b.path("members"))
                try:
                    assert entered.wait(15);wait_for(b.w,"transactionid")
                    blocker.execute(text("DELETE FROM collection_items WHERE collection_id=:c"),{"c":b.collection})
                    blocker.commit()
                finally:blocker.rollback()
                r=reader.result(20)
            assert r.status_code==200 and r.json()["total"]==0 and r.json()["items"]==[]
    assert b.w.runtime.pool.checkedout()==0


def test_only_permitted_page_rows_locked_not_count_scope(resources,monkeypatch):
    b=resources;other=accept(b.w,issue(b.w));foreign=source(b.w,other.id,"FOREIGN")
    with b.w.engine.begin() as conn:
        offpage=conn.scalar(text("INSERT INTO library_items(visibility,owner_user_id,title,created_at)"
             " VALUES('private',:u,'offpage','2020-01-01') RETURNING id"),{"u":b.w.uid})
    entered=threading.Event();release=threading.Event();real=resource_reads.final_caller
    def pause(db,**kw):
        proof=real(db,**kw)
        if not entered.is_set():entered.set();assert release.wait(15)
        return proof
    monkeypatch.setattr(resource_reads,"final_caller",pause)
    with ThreadPoolExecutor(1) as pool:
        reader=pool.submit(b.client.get,b.path("list")+"?scope=mine&limit=1")
        try:
            assert entered.wait(15)
            with b.w.engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout='1000ms'"))
                conn.execute(text("SELECT id FROM library_items WHERE id IN (:a,:b) ORDER BY id FOR UPDATE"),
                             {"a":foreign.item,"b":offpage}).all()
        finally:release.set()
        r=reader.result(20)
    assert r.status_code==200 and r.json()["total"]==2 and len(r.json()["items"])==1
    assert "FOREIGN" not in r.text
