"""M8b pilot operations: off-host journal replica, encrypted scheduled backups, alerts.

Nothing here is on the request path. The journal write path, the recovery gate
and readiness stay local (``app.journal``, ``app.recovery``): the replicator
only reads complete journal lines and ships them; backup/restore/check run as
one-shot commands driven by ``scripts/ras_ops.py``. Configuration comes from
one explicit JSON file (``RAS_OPS_CONFIG``) and secret files; never ``.env``.
"""
