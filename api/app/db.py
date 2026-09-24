from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings


def make_engine(database_url: str) -> Engine:
    # hide_parameters keeps bound values (e.g. password verifiers) out of
    # exception messages and SQL echo.
    return create_engine(database_url, pool_pre_ping=True, hide_parameters=True)


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def get_sessionmaker() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)
