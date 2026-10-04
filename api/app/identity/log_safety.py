"""Bound values and result rows must stay private even with SQL DEBUG enabled."""
import logging


class _PrivateRows(logging.Filter):
    def filter(self, record):
        # SQLAlchemy hide_parameters excludes binds but not DEBUG result rows.
        if record.msg == "Row %r":
            record.msg, record.args = "Row [private values omitted]", ()
        return True


def protect_auth_logs(db):
    protect_engine_logs(db.get_bind())


def protect_engine_logs(engine):
    engine.hide_parameters = True
    logger = engine.logger
    if not isinstance(logger, logging.Logger):
        logger = logger.logger
    if not any(isinstance(f, _PrivateRows) for f in logger.filters):
        logger.addFilter(_PrivateRows())
