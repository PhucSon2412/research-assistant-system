"""Runtime writers with real accounts, SQL gates, final clocks and PostgreSQL waits."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app import auth_safety, publication, recovery
from app.api import resource_writes, resource_reads
from app.api.errors import ApiError
from app.api.routes import library, collections
from app.db import get_engine
from app.authz import Principal
from app.identity.predicates import lock_users
from app.main import app
from app.models import Collection, CollectionItem, LibraryItem, UserSession
from app.models.enums import RevocationReason
from tests.test_c2d1_final_caller import browser, mutate, close, wait_for
from tests.test_c2c1_bootstrap import installation
from tests.test_c2c2_invitations import world, issue, accept
from tests.test_auth_safety_foundation import NOW
from tests.resource_fixtures import source, common_copy, resource_api, resource_world

ROUTES = {
    'item': ('PATCH', '/library/items/{item}', {'title': 'NEW-TITLE'}, 200),
    'create': ('POST', '/collections', {'name': 'NEW-NAME'}, 201),
    'rename': ('PATCH', '/collections/{collection}', {'name': 'NEW-NAME'}, 200),
    'delete': ('DELETE', '/collections/{collection}', None, 204),
    'add': ('POST', '/collections/{collection}/items', None, 201),
    'remove': ('DELETE', '/collections/{collection}/items/{item}', None, 204),
}


@pytest.fixture
def writes(browser):
    b = browser
    b.ids = source(b.w, b.w.uid, 'OLD-TITLE')
    with b.w.engine.begin() as conn:
        b.collection = conn.scalar(text("INSERT INTO collections(owner_user_id,name) VALUES(:u,'OLD-NAME') RETURNING id"), {'u': b.w.uid})
        conn.execute(text('INSERT INTO collection_items(collection_id,item_id) VALUES(:c,:i)'), {'c': b.collection, 'i': b.ids.item})
    b.path = lambda key: ROUTES[key][1].format(item=b.ids.item, collection=b.collection)
    return b


def request(b, key):
    method, _, body, _ = ROUTES[key]
    b.client.cookies.set('ras_csrf', b.w.actor.csrf_token)
    return b.client.request(method, b.path(key), json={'item_id': str(b.ids.item)} if key == 'add' else body,
        headers={'Origin': 'http://web.test', 'X-CSRF-Token': b.w.actor.csrf_token})


def business(b):
    with b.w.engine.connect() as conn:
        return {table: conn.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all()
            for table in ('library_items', 'paper_versions', 'source_files', 'collections', 'collection_items')}


def audits(b):
    with b.w.engine.connect() as conn:
        return conn.scalar(text("SELECT count(*) FROM audit_events WHERE action='library.item.update'"))


def changed(b, key, before):
    after = business(b)
    assert after['paper_versions'] == before['paper_versions']
    assert after['source_files'] == before['source_files']
    if key == 'add': assert after == before  # repeated add dedupes
    else: assert after != before


def api_pool_clean():
    assert get_engine().pool.checkedout() == 0


@pytest.mark.parametrize('key', ROUTES)
def test_six_success_new_transaction_flush_exclusive_and_cleanup(writes, monkeypatch, key, caplog):
    b = writes; before = business(b); journal = b.w.path.read_bytes(); guards = []; actor_calls = []
    real = auth_safety.require_final; actor = resource_writes._actor
    def guarded(db, **kw):
        if kw.get('writer'):
            assert not db.new and not db.dirty and not db.deleted
        result = real(db, **kw)
        guards.append((db.get_transaction(), kw.get('writer', False)))
        return result
    def checked(db, *args):
        actor_calls.append(db.get_transaction())
        return actor(db, *args)
    monkeypatch.setattr(auth_safety, 'require_final', guarded)
    monkeypatch.setattr(resource_writes, '_actor', checked)
    r = request(b, key)
    assert r.status_code == ROUTES[key][3], r.text
    assert len(actor_calls) == 4 and actor_calls[-1] is not actor_calls[-2]
    assert [writer for _, writer in guards] == ([True, False] if key in ('delete', 'remove') else [True])
    # resource_json imports its own shared final helper; actor call still proves new transaction.
    assert all(not tx.is_active for tx in actor_calls)
    if key in ('delete', 'remove'): assert not r.content
    if key == 'item': assert r.json()['item']['title'] == 'NEW-TITLE'
    if key in ('create', 'rename'): assert r.json()['collection']['name'] == 'NEW-NAME'
    changed(b, key, before)
    assert b.w.path.read_bytes() == journal
    with b.w.runtime.connect() as conn:
        assert conn.scalar(text('SELECT last_activity_at FROM sessions WHERE id=:s'), {'s': b.w.actor.session_id}) == NOW
        audit = ' '.join(conn.execute(text('SELECT row_to_json(a)::text FROM audit_events a')).scalars())
    for secret in (b.w.actor.token, b.w.actor.csrf_token, 'NEW-TITLE', 'NEW-NAME'):
        assert secret not in caplog.text + audit + b.w.path.read_text()
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
@pytest.mark.parametrize('change', ['revoke', 'disable', 'generation', 'quarantine', 'held',
    'receipt_credential', 'receipt_status', 'wrong_receipt', 'head', 'latest_credential',
    'latest_status', 'hold', 'ahead', 'unconfirmed', 'csrf'])
def test_after_dependency_auth_changes_rollback_all_business(writes, key, change):
    b = writes; before = business(b); audit = audits(b)
    def alter(db, ctx):
        if change == 'csrf':
            with b.w.engine.begin() as conn:
                conn.execute(text("UPDATE sessions SET csrf_token_hash=repeat('f',64) WHERE id=:s"), {'s': ctx.session_id})
        else: mutate(b, change)
    b.after_dependency = alter
    r = request(b, key)
    expected = 503 if change in ('hold', 'ahead', 'unconfirmed') else 403 if change == 'csrf' else 401
    assert r.status_code == expected, r.text
    assert business(b) == before and audits(b) == audit
    assert not any(marker in r.text for marker in ('NEW-TITLE', 'NEW-NAME', str(b.ids.item)))
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
@pytest.mark.parametrize('kind,seconds,expected', [('idle',1799,None), ('idle',1800,401),
    ('absolute',28799,None), ('absolute',28800,401)])
def test_clock_after_dml_and_before_commit(writes, monkeypatch, key, kind, seconds, expected):
    b = writes; before = business(b); audit = audits(b); real = auth_safety.require_final
    if kind == 'absolute':
        def activity(db, ctx):
            with b.w.engine.begin() as conn:
                conn.execute(text('UPDATE sessions SET last_activity_at=:at WHERE id=:s'),
                    {'at': NOW+timedelta(hours=7,minutes=59), 's': ctx.session_id})
        b.after_dependency = activity
    def gate(db, **kw):
        result = real(db, **kw)
        if kw.get('writer'): b.at = NOW+timedelta(seconds=seconds)
        return result
    monkeypatch.setattr(auth_safety, 'require_final', gate)
    r = request(b, key)
    assert r.status_code == (expected or ROUTES[key][3]), r.text
    if expected: assert business(b) == before and audits(b) == audit
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
def test_real_pending_six_no_mutation(installation, key):
    w = installation
    with w.runtime.connect() as conn:
        before = {table: conn.scalar(text(f'SELECT count(*) FROM {table}'))
            for table in ('collections', 'collection_items', 'library_items', 'audit_events')}
    with TestClient(app) as client:
        method, path, body, _ = ROUTES[key]
        r = client.request(method, path.format(item=uuid.uuid4(), collection=uuid.uuid4()),
            json={'item_id': str(uuid.uuid4())} if key == 'add' else body,
            headers={'Origin':'http://web.test'})
        assert r.status_code == 503
        assert client.get('/healthz').status_code == 200
        assert client.get('/readyz').status_code == 503
    with w.runtime.connect() as conn:
        assert {table: conn.scalar(text(f'SELECT count(*) FROM {table}')) for table in before} == before
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
@pytest.mark.parametrize('landed', [False, True])
def test_commit_lost_reply_never_success_or_retry(writes, monkeypatch, key, landed):
    b = writes; before = business(b); real = Session.commit; calls = []
    def commit(db):
        if db.get_bind() is get_engine():
            calls.append(db)
            if len(calls) == 2:  # activity dependency is the first commit
                real(db) if landed else db.rollback()
                raise RuntimeError('private SQL/URL lost reply')
        return real(db)
    monkeypatch.setattr(Session, 'commit', commit)
    r = request(b, key)
    assert r.status_code == 503 and r.json() == {'error':'resource_write_outcome_uncertain'}
    assert len(calls) == 2
    if landed: changed(b, key, before)
    else: assert business(b) == before
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
def test_postcommit_revocation_blocks_payload_but_mutation_landed(writes, monkeypatch, key):
    b = writes; before = business(b); real = Session.commit; calls = []
    def commit(db):
        result = real(db)
        if db.get_bind() is get_engine():
            calls.append(1)
            if len(calls) == 2: close(b.w)
        return result
    monkeypatch.setattr(Session, 'commit', commit)
    r = request(b, key)
    assert r.status_code == 401 and r.json() == {'error':'session_expired'}, r.text
    changed(b, key, before)
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
def test_precommit_guard_denial_after_flush_rolls_back(writes, monkeypatch, key):
    b = writes; before = business(b); audit = audits(b); real = auth_safety.require_final
    def deny(db, **kw):
        real(db, **kw)
        if kw.get('writer'): raise ApiError(503, 'service_recovering')
    monkeypatch.setattr(auth_safety, 'require_final', deny)
    r = request(b, key)
    assert r.status_code == 503 and r.json() == {'error':'service_recovering'}
    assert business(b) == before and audits(b) == audit
    api_pool_clean()


def test_current_role_after_dependency_prevents_common_edit(writes):
    b = writes; common = common_copy(b.w, indexed=False)
    b.ids = common; before = business(b); audit = audits(b)
    b.after_dependency = lambda db, ctx: mutate(b, 'role')
    r = request(b, 'item')
    assert r.status_code == 403 and r.json() == {'error':'forbidden'}
    assert business(b) == before and audits(b) == audit
    api_pool_clean()


def test_exact_session_owner_binding_after_dependency(writes):
    b = writes; other = accept(b.w, issue(b.w)); before = business(b)
    def rebind(db, ctx):
        with b.w.engine.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role='replica'"))
            conn.execute(text('UPDATE sessions SET user_id=:u,credential_generation=:g WHERE id=:s'),
                {'u':other.id, 'g':other.credential_generation, 's':ctx.session_id})
    b.after_dependency = rebind
    r = request(b,'item')
    assert r.status_code == 401 and business(b) == before


@pytest.mark.parametrize('key', ['item','rename','create','add'])
def test_postcommit_resource_changed_does_not_return_old_dto(writes, monkeypatch, key):
    b = writes; real = Session.commit; calls = []
    def commit(db):
        real(db)
        if db.get_bind() is get_engine():
            calls.append(1)
            if len(calls) == 2:
                with b.w.engine.begin() as conn:
                    if key == 'item':
                        conn.execute(text("UPDATE library_items SET title='FRESH-TITLE',content_status='indexed' WHERE id=:i"),{'i':b.ids.item})
                    elif key in ('rename','create'):
                        conn.execute(text("UPDATE collections SET name='FRESH-NAME' WHERE owner_user_id=:u"), {'u':b.w.uid})
                    else:
                        conn.execute(text('DELETE FROM collection_items WHERE collection_id=:c AND item_id=:i'), {'c':b.collection,'i':b.ids.item})
    monkeypatch.setattr(Session,'commit',commit)
    r = request(b,key)
    if key == 'add': assert r.status_code == 409 and r.json() == {'error':'resource_scope_changed'}
    elif key == 'item':
        assert r.json()['item']['title'] == 'FRESH-TITLE'
        assert r.json()['item']['fulltext_search_available'] and r.json()['item']['rag_available']
    else: assert r.json()['collection']['name'] == 'FRESH-NAME'
    api_pool_clean()


def test_remove_hidden_membership_and_missing_preserves_item(writes):
    b = writes; common = common_copy(b.w, indexed=False)
    with b.w.engine.begin() as conn:
        conn.execute(text('INSERT INTO collection_items(collection_id,item_id) VALUES(:c,:i)'), {'c':b.collection,'i':common.item})
    with Session(b.w.engine) as db:
        publication.revoke(db, common.admin, common.item, RevocationReason.RIGHTS_WITHDRAWN, NOW, timedelta(hours=24))
    b.ids = common; before = business(b)
    r = request(b,'remove')
    assert r.status_code == 204 and not r.content
    assert business(b)['library_items'] == before['library_items']
    assert request(b,'remove').json() == {'error':'not_found'}


@pytest.mark.parametrize('key', ROUTES)
def test_precommit_sql_failure_controlled_cleanup(writes, monkeypatch, key):
    b = writes; before = business(b); real = auth_safety.require_final
    def broken(db, **kw):
        if kw.get('writer'): db.execute(text('SELECT private_sql_missing_function()'))
        return real(db, **kw)
    monkeypatch.setattr(auth_safety,'require_final',broken)
    r = request(b,key)
    assert r.status_code == 503 and r.json() == {'error':'resource_write_unavailable'}
    assert business(b) == before
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
@pytest.mark.parametrize('change', ['generation', 'csrf', 'receipt_status', 'hold', 'bad_journal', 'owner'])
def test_final_guard_after_flush_real_predicates(writes, monkeypatch, key, change):
    b = writes; before = business(b); audit = audits(b); real_write = resource_writes.resource_write; real_flush = Session.flush
    other = accept(b.w, issue(b.w)) if change=='owner' else None
    def boundary(db, principal, **kw):
        operation = kw['mutate']
        def prepared(current):
            result = operation(current); db.info['inject_resource_after_flush'] = True
            return result
        return real_write(db, principal, **(kw | {'mutate': prepared}))
    def flushed(db, *args, **kw):
        result = real_flush(db, *args, **kw)
        if db.info.pop('inject_resource_after_flush',False):
            assert not db.new and not db.dirty and not db.deleted
            if change == 'hold': recovery.hold(b.w.engine, b.w.settings, NOW)
            elif change == 'bad_journal': b.w.path.write_bytes(b.w.path.read_bytes()+b'bad-chain\n')
            elif change == 'csrf':
                with b.w.engine.begin() as conn:
                    conn.execute(text("UPDATE sessions SET csrf_token_hash=repeat('f',64) WHERE id=:s"), {'s':b.w.actor.session_id})
            elif change == 'owner':
                with b.w.engine.begin() as conn:
                    conn.execute(text("SET LOCAL session_replication_role='replica'"))
                    conn.execute(text('UPDATE sessions SET user_id=:u,credential_generation=:g WHERE id=:s'),
                        {'u':other.id,'g':other.credential_generation,'s':b.w.actor.session_id})
            else: mutate(b, change)
        return result
    monkeypatch.setattr(library if key=='item' else collections,'resource_write',boundary)
    monkeypatch.setattr(Session,'flush',flushed)
    r = request(b,key)
    expected = 503 if change in ('hold','bad_journal') else 403 if change == 'csrf' else 401
    assert r.status_code == expected, r.text
    assert business(b) == before and audits(b) == audit
    api_pool_clean()


@pytest.mark.parametrize('change', ['role', 'generation_state'])
def test_postcommit_common_role_and_fulltext_flags_are_fresh(writes,monkeypatch,change):
    b=writes;b.ids=common_copy(b.w,indexed=True,provider=True);real=Session.commit;calls=[]
    def commit(db):
        real(db)
        if db.get_bind() is get_engine():
            calls.append(1)
            if len(calls)==2:
                if change=='role': mutate(b,'role')
                else:
                    with b.w.engine.begin() as conn:
                        conn.execute(text("UPDATE index_generations SET state='superseded' WHERE item_id=:i"),{'i':b.ids.item})
    monkeypatch.setattr(Session,'commit',commit)
    r=request(b,'item')
    assert r.status_code==200,r.text
    row=r.json()['item']
    assert row['title']=='NEW-TITLE' and row['visibility']=='common'
    assert row['can_edit']==(change!='role')
    assert row['fulltext_search_available']==(change=='role')
    assert row['rag_available']==(change=='role')
    api_pool_clean()


@pytest.mark.parametrize('state', ['pending','revoked'])
def test_add_ordinary_gate_denies_pending_and_revoked(writes,state):
    b=writes;b.ids=common_copy(b.w,indexed=False,pending=state=='pending')
    if state=='revoked':
        with Session(b.w.engine) as db:
            publication.revoke(db,b.ids.admin,b.ids.item,RevocationReason.RIGHTS_WITHDRAWN,NOW,timedelta(hours=24))
    before=business(b)
    r=request(b,'add')
    assert r.status_code==404 and r.json()=={'error':'not_found'}
    assert business(b)==before
    api_pool_clean()


@pytest.mark.parametrize('key', ROUTES)
def test_sql_order_fk_flush_before_exclusive_no_lock_after_journal(writes, monkeypatch, key):
    b = writes; statements = []; started = []; real = resource_writes.lock_users
    def locked(db, ids):
        result = real(db, ids)
        started.append(db.get_transaction())
        return result
    def sql(conn, cursor, statement, parameters, context, executemany):
        if started: statements.append(statement.lower())
    monkeypatch.setattr(resource_writes,'lock_users',locked)
    eng = get_engine(); event.listen(eng,'before_cursor_execute',sql)
    try: r = request(b,key)
    finally: event.remove(eng,'before_cursor_execute',sql)
    assert r.status_code == ROUTES[key][3], r.text
    j = next(i for i,s in enumerate(statements) if 'pg_advisory_xact_lock(' in s)
    pre, post = statements[:j], statements[j+1:]
    # Commit itself has no SQL event: only check until first postcommit resource lock.
    if key == 'add':
        item = next(i for i,s in enumerate(pre) if 'from library_items' in s and 'for share' in s)
        collection = next(i for i,s in enumerate(pre) if 'from collections' in s and 'for update' in s)
        insert = next(i for i,s in enumerate(pre) if 'insert into collection_items' in s)
        assert item < collection < insert < j
    if key == 'item': assert any('insert into audit_events' in s for s in pre)
    if key == 'create': assert any('insert into collections' in s for s in pre)
    assert not any('ras_auth_lock_users' in s for s in post)
    assert not any(s.startswith(('insert ', 'update ', 'delete ')) for s in post)
    assert not any('for update' in s or 'for key share' in s for s in post)
    api_pool_clean()


@pytest.mark.parametrize('lock_kind', ['user','item','collection','journal'])
@pytest.mark.parametrize('expired', [False,True])
def test_observed_wait_then_fresh_clock(writes, monkeypatch, lock_kind, expired):
    b = writes; before = business(b); dep = threading.Event(); proceed = threading.Event()
    def dependency(db, ctx):
        dep.set(); assert proceed.wait(10)
    b.after_dependency = dependency
    key = 'rename' if lock_kind == 'collection' else 'item'
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(request,b,key)
        assert dep.wait(10)
        with Session(b.w.runtime) as blocker:
            if lock_kind == 'user': lock_users(blocker,[b.w.uid])
            elif lock_kind == 'item': blocker.execute(select(LibraryItem.id).where(LibraryItem.id==b.ids.item).with_for_update())
            elif lock_kind == 'collection': blocker.execute(select(Collection.id).where(Collection.id==b.collection).with_for_update())
            else: blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'),{'k':recovery.JOURNAL_LOCK})
            proceed.set()
            wait_for(b.w, 'advisory' if lock_kind == 'journal' else 'transactionid')
            b.at = NOW+timedelta(seconds=1800 if expired else 1799)
            blocker.rollback()
        r = future.result(10)
    assert r.status_code == (401 if expired else ROUTES[key][3]), r.text
    if expired: assert business(b) == before
    api_pool_clean()


@pytest.mark.parametrize('key,lock_kind', [('item','item'),('rename','collection'),('item','journal')])
def test_lock_timeout_controlled_and_pool_returned(writes, key, lock_kind):
    b = writes; before = business(b); dep = threading.Event(); proceed = threading.Event()
    b.after_dependency = lambda db,ctx: (dep.set(), proceed.wait(10))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(request,b,key); assert dep.wait(10)
        with Session(b.w.runtime) as blocker:
            if lock_kind == 'journal': blocker.execute(text('SELECT pg_advisory_xact_lock_shared(:k)'),{'k':recovery.JOURNAL_LOCK})
            elif lock_kind == 'item': blocker.execute(select(LibraryItem.id).where(LibraryItem.id==b.ids.item).with_for_update())
            else: blocker.execute(select(Collection.id).where(Collection.id==b.collection).with_for_update())
            proceed.set(); wait_for(b.w,'advisory' if lock_kind=='journal' else 'transactionid')
            r = future.result(10)
    assert r.status_code == 503
    assert r.json() == {'error':'service_recovering' if lock_kind=='journal' else 'resource_write_unavailable'}
    assert business(b) == before
    api_pool_clean()


@pytest.mark.parametrize('winner', ['reader','writer'])
@pytest.mark.parametrize('key', ['item','rename','delete','remove','add'])
def test_writer_reader_two_lock_orders(writes, monkeypatch, winner, key):
    b = writes; reached = threading.Event(); release = threading.Event(); p = Principal(b.w.uid,'admin',b.w.actor.session_id,'admin@example.test',None)
    real = resource_reads.final_caller if winner=='reader' else auth_safety.require_final
    def pause(db, **kw):
        if winner=='reader' or kw.get('writer'):
            if not reached.is_set():
                reached.set(); assert release.wait(10)
        return real(db, **kw)
    if winner=='reader': monkeypatch.setattr(resource_reads,'final_caller',pause)
    else: monkeypatch.setattr(auth_safety,'require_final',pause)
    def reader():
        with Session(b.w.runtime) as db:
            try:
                if key=='item': result = library.get_item(str(b.ids.item),p,db,clock=b.clock,settings=b.w.settings)
                elif key in ('delete','remove','add'): result = collections.list_collection_items(b.collection,20,0,p,db,clock=b.clock,settings=b.w.settings)
                else: result = collections.list_collections(p,db,clock=b.clock,settings=b.w.settings)
                return result
            except ApiError as exc: return exc.code
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(reader if winner=='reader' else request, *(() if winner=='reader' else (b,key)))
        assert reached.wait(10)
        second = pool.submit(request if winner=='reader' else reader, *((b,key) if winner=='reader' else ()))
        try: wait_for(b.w,'transactionid')
        finally: release.set()
        initial, later = first.result(10),second.result(10)
    r = later if winner=='reader' else initial
    assert r.status_code == ROUTES[key][3], r.text
    result = initial if winner=='reader' else later
    if winner=='writer' and key=='delete': assert result=='not_found'
    elif key=='item': assert result['item']['title']==('OLD-TITLE' if winner=='reader' else 'NEW-TITLE')
    api_pool_clean()


@pytest.mark.parametrize('winner', ['auth','writer'])
def test_auth_closure_writer_two_legal_orders(writes, monkeypatch, winner):
    b = writes; before = business(b); reached=threading.Event(); release=threading.Event(); real=auth_safety.require_final
    if winner=='writer':
        def pause(db, **kw):
            if kw.get('writer') and db.get_bind() is get_engine():
                reached.set(); assert release.wait(10)
            return real(db, **kw)
        monkeypatch.setattr(auth_safety,'require_final',pause)
        with ThreadPoolExecutor(2) as pool:
            writer=pool.submit(request,b,'item'); assert reached.wait(10)
            closer=pool.submit(close,b.w)
            try: wait_for(b.w,'transactionid')
            finally: release.set()
            r=writer.result(10); closer.result(10)
        # Closure may win the new postcommit proof; either allowed boundary is valid.
        assert r.status_code in (200,401),r.text
        changed(b,'item',before)
        assert b.client.get('/auth/session').status_code==401
    else:
        def dependency(db,ctx): reached.set(); assert release.wait(10)
        b.after_dependency=dependency
        with ThreadPoolExecutor(1) as pool:
            writer=pool.submit(request,b,'item'); assert reached.wait(10)
            close(b.w); release.set(); r=writer.result(10)
        assert r.status_code==401 and business(b)==before
    api_pool_clean()


@pytest.mark.parametrize('winner', ['add','revoke'])
def test_add_revoke_cleanup_two_legal_orders(writes, monkeypatch, winner):
    b=writes; common=common_copy(b.w,indexed=False); b.ids=common
    reached=threading.Event();release=threading.Event();real=auth_safety.require_final
    def revoke():
        with Session(b.w.engine) as db:
            db.execute(text("SET LOCAL application_name='c2d3_revoke'"))
            publication.revoke(db,common.admin,common.item,RevocationReason.RIGHTS_WITHDRAWN,NOW,timedelta(hours=24))
    if winner=='add':
        def pause(db,**kw):
            if kw.get('writer') and db.get_bind() is get_engine(): reached.set();assert release.wait(10)
            return real(db,**kw)
        monkeypatch.setattr(auth_safety,'require_final',pause)
        with ThreadPoolExecutor(2) as pool:
            adding=pool.submit(request,b,'add');assert reached.wait(10)
            revoking=pool.submit(revoke)
            try: wait_for_named(b.w, 'c2d3_revoke', 'transactionid')
            finally: release.set()
            r=adding.result(10);revoking.result(10)
        assert r.status_code in (201,409),r.text
        with b.w.engine.connect() as conn:
            assert conn.scalar(text('SELECT count(*) FROM collection_items WHERE collection_id=:c AND item_id=:i'),{'c':b.collection,'i':common.item})==1
    else:
        b.after_dependency=lambda db,ctx:(reached.set(),release.wait(10))
        with ThreadPoolExecutor(1) as pool:
            adding=pool.submit(request,b,'add');assert reached.wait(10)
            revoke();release.set();r=adding.result(10)
        assert r.status_code==404,r.text
    class EmptyStorage:
        removed = False
        def size(self,key):
            if self.removed:
                from botocore.exceptions import ClientError
                raise ClientError({'Error':{'Code':'NoSuchKey'}},'HeadObject')
            return 1
        def delete(self,key): self.removed = True
    with Session(b.w.engine) as db:
        tid=db.scalar(text('SELECT id FROM deletion_tombstones WHERE item_id=:i'),{'i':common.item})
        publication._purge_one(db,EmptyStorage(),tid,NOW)
    with b.w.engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM collection_items WHERE item_id=:i'),{'i':common.item})==0
        assert conn.scalar(text('SELECT count(*) FROM library_items WHERE id=:i'),{'i':b.ids.item})==1
    api_pool_clean()


def wait_for_named(w, name, wait):
    deadline=time.monotonic()+10
    with w.engine.connect() as observer:
        while time.monotonic()<deadline:
            found=observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND application_name=:n AND wait_event=:e)"),{'n':name,'e':wait})
            observer.rollback()
            if found: return
            threading.Event().wait(.005)
    raise AssertionError('named PostgreSQL wait not observed')


@pytest.mark.parametrize('other', ['add','remove'])
@pytest.mark.parametrize('winner', ['delete','other'])
def test_delete_collection_vs_membership_two_orders(writes,monkeypatch,other,winner):
    b=writes;before=business(b);reached=threading.Event();release=threading.Event();once=[];real=auth_safety.require_final
    def pause(db,**kw):
        if kw.get('writer') and not once:
            once.append(1);reached.set();assert release.wait(10)
        return real(db,**kw)
    monkeypatch.setattr(auth_safety,'require_final',pause)
    firstkey='delete' if winner=='delete' else other
    secondkey=other if winner=='delete' else 'delete'
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(request,b,firstkey);assert reached.wait(10)
        second=pool.submit(request,b,secondkey)
        try: wait_for(b.w,'transactionid')
        finally: release.set()
        r1,r2=first.result(10),second.result(10)
    assert r1.status_code==ROUTES[firstkey][3],r1.text
    assert r2.status_code==(404 if winner=='delete' else 204),r2.text
    after=business(b)
    assert after['collections']==[] and after['collection_items']==[]
    assert after['library_items']==before['library_items']
    assert after['paper_versions']==before['paper_versions'] and after['source_files']==before['source_files']
    api_pool_clean()


@pytest.mark.parametrize('key', ['item','rename','create','add','remove'])
def test_clock_after_last_resource_gate_sql(writes,monkeypatch,key):
    b=writes;before=business(b);audit=audits(b);real=resource_writes.Mutation
    def mutation(*args,**kw):
        result=real(*args,**kw);check=result.check
        def last(current):
            check(current);b.at=NOW+timedelta(minutes=30)
        result.check=last
        return result
    monkeypatch.setattr(library if key=='item' else collections,'Mutation',mutation)
    r=request(b,key)
    assert r.status_code==401 and r.json()=={'error':'session_expired'},r.text
    assert business(b)==before and audits(b)==audit
    api_pool_clean()


@pytest.mark.parametrize('key', ['item','rename','add'])
def test_postcommit_scope_loss_no_stale_private_payload(writes,monkeypatch,key):
    b=writes;other=accept(b.w,issue(b.w));real=Session.commit;calls=[]
    if key=='item': b.ids=common_copy(b.w,indexed=False)
    def commit(db):
        real(db)
        if db.get_bind() is get_engine():
            calls.append(1)
            if len(calls)==2:
                if key=='item':
                    with Session(b.w.engine) as revoking:
                        publication.revoke(revoking,b.ids.admin,b.ids.item,RevocationReason.RIGHTS_WITHDRAWN,NOW,timedelta(hours=24))
                else:
                    with b.w.engine.begin() as conn:
                        conn.execute(text('UPDATE collections SET owner_user_id=:u WHERE id=:i'),
                            {'u':other.id,'i':b.collection})
    monkeypatch.setattr(Session,'commit',commit)
    r=request(b,key)
    assert r.status_code==409 and r.json()=={'error':'resource_scope_changed'},r.text
    with b.w.engine.connect() as conn:
        if key=='item': assert conn.scalar(text('SELECT title FROM library_items WHERE id=:i'),{'i':b.ids.item})=='NEW-TITLE'
        elif key=='rename': assert conn.scalar(text('SELECT name FROM collections WHERE id=:i'),{'i':b.collection})=='NEW-NAME'
        else: assert conn.scalar(text('SELECT count(*) FROM collection_items WHERE collection_id=:i AND item_id=:p'),{'i':b.collection,'p':b.ids.item})==1
    api_pool_clean()
