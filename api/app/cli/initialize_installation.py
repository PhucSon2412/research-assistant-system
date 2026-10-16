"""Compose new-install reconciliation; never automatically recovers an existing DB."""
from sqlalchemy import text
from app import recovery
from app.clock import utcnow
from app.config import get_settings
from app.db import get_engine
from app.storage import get_storage


def main():
    engine, settings = get_engine(), get_settings()
    with engine.connect() as conn:
        registered = conn.scalar(text('SELECT EXISTS(SELECT 1 FROM recovery_registrations)'))
    if registered:
        status = recovery.evaluate(settings, engine, administrative=True)
        print('existing installation: gate=' + status.reason)
        if not status.open:
            print('maintenance required: stop writers; hold before migrate; provision; recover; verify runtime readiness')
        return
    try:
        recovery.init_new(engine, settings, utcnow())
    except recovery.RecoveryError as exc:
        if exc.code in ('database_not_new', 'journal_exists'):
            print('initialization refused: ' + exc.code + '; explicit registration/recovery required')
            return
        raise
    storage = get_storage(settings)
    storage.ensure_bucket()
    result = recovery.recover(engine, storage, settings, utcnow())
    if not result.safety_ready:
        raise recovery.RecoveryError('new_install_contract_incomplete')
    print('new installation reconciled; runtime bootstrap available once')


if __name__ == '__main__':
    main()
