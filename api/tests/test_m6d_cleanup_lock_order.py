"""Real runtime/PG waits: repeat deletion and private scrub must serialize safely.

The existing M6d fixture supplies genuine C4 readiness/journal and labelled fake
object storage. No timeout, clock, guard or PostgreSQL setting is changed here.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import private_cleanup, recovery
from app.api import private_delete as boundary
from app.config import get_settings
from app.db import get_engine
from tests.test_m6d_private_delete import (  # noqa: F401 -- fixture dependencies
    installation, world, browser, writers, deletion, opened, remove, direct, purge, scalar,
)
from tests.test_c2d5a_review_writes import request as review_request


def history(b, count):
    ids=[]
    for number in range(count):
        ids.append(opened(b))
        if number<count-1:
            response=review_request(b,'withdraw')
            assert response.status_code==200,response.text
    return ids


class Locks:
    """Observation only; capture backend PIDs without issuing worker SQL."""
    def __init__(self,b):
        self.b=b;self.pids={};self.errors=[]
        self.engines={b.w.runtime,get_engine()}

    def begin(self,session,transaction,connection):
        if session.info.get('private_deletion_phase')=='accept':role='repeat'
        elif session.info.get('private_cleanup_phase')=='finalize':role='cleanup'
        else:return
        self.pids[role]=connection.connection.driver_connection.info.backend_pid

    def error(self,context):
        self.errors.append({'sqlstate':getattr(context.original_exception,'sqlstate',None),
            'type':type(context.original_exception).__name__})

    def __enter__(self):
        event.listen(Session,'after_begin',self.begin)
        for engine in self.engines:
            event.listen(engine,'handle_error',self.error)
        return self

    def __exit__(self,*_):
        event.remove(Session,'after_begin',self.begin)
        for engine in self.engines:
            event.remove(engine,'handle_error',self.error)
        assert not self.errors,{'postgresql_errors':self.errors}

    def call(self,role,operation):
        return operation(self.b)

    def blocked_by(self,waiting,holding):
        deadline=time.monotonic()+10
        with self.b.w.engine.connect() as observer:
            while time.monotonic()<deadline:
                if waiting in self.pids and holding in self.pids:
                    found=observer.scalar(text("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                        WHERE pid=:waiting AND wait_event='transactionid'
                          AND :holding=ANY(pg_blocking_pids(pid)))"""),
                        {'waiting':self.pids[waiting],'holding':self.pids[holding]})
                    observer.rollback()
                    if found:return
                threading.Event().wait(.005)
        raise AssertionError('expected backend-to-backend transaction wait not observed')


def receipt(b,mode):
    if mode=='boundary':return direct(b)
    response=remove(b)
    assert response.status_code==200,(response.status_code,response.text)
    return response.json()


def finished(b,ids,before,cleanup,result,errors):
    assert cleanup=={'purged':1},(cleanup,errors)
    assert result['deleted'] is True and result['item_id']==str(b.own.item),result
    assert not errors,errors  # includes a real 40P01 if a cycle returns
    assert remove(b).json()['cleanup_status']=='completed'
    assert b.w.path.read_bytes()==before
    assert scalar(b,'SELECT rights_epoch FROM library_items WHERE id=:i',i=b.own.item)==1
    assert scalar(b,'SELECT count(*) FROM private_item_deletions WHERE item_id=:i AND purged_at IS NOT NULL',i=b.own.item)==1
    assert scalar(b,'SELECT title FROM library_items WHERE id=:i',i=b.own.item) is None
    with b.w.engine.connect() as db:
        rows=db.execute(text('SELECT id,status,num_nonnulls(declared_source,declared_rights,declared_source_url,'
            'declared_license_url,rejection_note) FROM review_requests WHERE item_id=:i'),{'i':b.own.item}).all()
    assert {r.id for r in rows}==set(ids)
    assert all(r.status=='withdrawn' and r[2]==0 for r in rows),rows
    assert recovery.evaluate(get_settings(),b.w.runtime).open
    assert b.client.get('/readyz').status_code==200
    assert get_engine().pool.checkedout()==0


@pytest.mark.parametrize('mode',['http','boundary'])
@pytest.mark.parametrize('reviews',[0,1,3])
def test_cleanup_item_lock_then_repeat_both_complete(deletion,monkeypatch,mode,reviews):
    b=deletion;ids=history(b,reviews)
    assert remove(b).status_code==200
    before=b.w.path.read_bytes();entered,release=threading.Event(),threading.Event()
    original=private_cleanup.scrub
    def paused(db,*args):
        # finalize holds item UPDATE here; the fixed path also owns its reviews.
        entered.set()
        assert release.wait(20),'cleanup scrub barrier was not released'
        return original(db,*args)
    monkeypatch.setattr(private_cleanup,'scrub',paused)
    with Locks(b) as locks,ThreadPoolExecutor(2) as pool:
        cleanup=pool.submit(locks.call,'cleanup',purge)
        try:
            assert entered.wait(20),'cleanup did not reach scrub'
            repeat=pool.submit(locks.call,'repeat',lambda b:receipt(b,mode))
            locks.blocked_by('repeat','cleanup')
        finally:release.set()
        clean_result=cleanup.result(timeout=20)
        repeat_result=repeat.result(timeout=20)
    finished(b,ids,before,clean_result,repeat_result,locks.errors)


@pytest.mark.parametrize('reviews',[0,3])
def test_repeat_locks_before_cleanup_both_complete(deletion,monkeypatch,reviews):
    b=deletion;ids=history(b,reviews)
    assert remove(b).status_code==200
    before=b.w.path.read_bytes();entered,release=threading.Event(),threading.Event()
    original=boundary._gate
    def paused(db,*args,**kwargs):
        # Repeat already holds review -> item, before its final journal proof.
        entered.set()
        assert release.wait(20),'repeat proof barrier was not released'
        return original(db,*args,**kwargs)
    monkeypatch.setattr(boundary,'_gate',paused)
    with Locks(b) as locks,ThreadPoolExecutor(2) as pool:
        repeat=pool.submit(locks.call,'repeat',direct)
        try:
            assert entered.wait(20),'repeat did not reach final proof'
            cleanup=pool.submit(locks.call,'cleanup',purge)
            locks.blocked_by('cleanup','repeat')
        finally:release.set()
        repeat_result=repeat.result(timeout=20)
        clean_result=cleanup.result(timeout=20)
    finished(b,ids,before,clean_result,repeat_result,locks.errors)


def test_concurrent_first_delete_with_review_history_is_one_decision(deletion):
    b=deletion;ids=history(b,3)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:direct(b),range(2)))
    assert all(r['deleted'] is True for r in results)
    assert scalar(b,"SELECT count(*) FROM revocation_journal_records WHERE kind='private_deletion'")==1
    before=b.w.path.read_bytes()
    finished(b,ids,before,purge(b),remove(b).json(),[])
