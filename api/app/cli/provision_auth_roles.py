"""Explicit migration-time grants; connection comes only from RAS_DB__* settings."""
import argparse
import os
import psycopg
from psycopg import sql
from app.db import get_engine
from app.db_roles import provision


def create_logins(engine, names, definer):
    """Deployment-owned roles only; existing passwords are never rotated here."""
    from app.db_roles import _identifier
    for name, _ in names:
        _identifier(name)
    _identifier(definer)
    with psycopg.connect(engine.url.render_as_string(hide_password=False).replace(
            'postgresql+psycopg://', 'postgresql://')) as conn:
        database = conn.execute('SELECT current_database()').fetchone()[0]
        marker = 'ras deployment database ' + database
        existing = conn.execute("SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (definer,)).fetchone()
        if existing is not None and existing != (marker,):
            raise ValueError('existing definer is not owned by this deployment')
        for name, variable in names:
            password = os.environ.get(variable)
            if not password:
                raise ValueError('deployment login credential required')
            existing = conn.execute("SELECT rolcanlogin,shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
            if existing:
                if existing != (True, marker):
                    raise ValueError('existing role is not owned by this deployment')
                continue
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(
                sql.Identifier(name), sql.Literal(password)))
            conn.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(name), sql.Literal(marker)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--recovery", required=True)
    parser.add_argument("--definer", required=True)
    parser.add_argument("--create-logins", action="store_true")
    args = parser.parse_args()
    engine = get_engine()
    if args.create_logins:
        create_logins(engine, [(args.runtime, 'RAS_PROVISION_RUNTIME_PASSWORD'),
                               (args.recovery, 'RAS_PROVISION_RECOVERY_PASSWORD')], args.definer)
    provision(engine, runtime=args.runtime, recovery=args.recovery, definer=args.definer)
    if args.create_logins:
        with psycopg.connect(engine.url.render_as_string(hide_password=False).replace(
                'postgresql+psycopg://', 'postgresql://')) as conn:
            database = conn.execute('SELECT current_database()').fetchone()[0]
            conn.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(sql.Identifier(args.definer),
                sql.Literal('ras deployment database '+database)))
    print("auth roles provisioned; reconciliation is required before readiness")


if __name__ == "__main__":
    main()
