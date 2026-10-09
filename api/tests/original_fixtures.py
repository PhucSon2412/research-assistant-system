"""Runtime HTTP resource setup; owner engine only for labelled setup/corruption."""
import pytest
from fastapi import Request
from sqlalchemy.orm import Session
from app.api.deps import get_db
from app.db import get_engine
from app.main import app
from tests.test_c2c1_bootstrap import installation
from tests.test_uploads import upload_api as base_upload_api


@pytest.fixture
def upload_api(base_upload_api, installation):
    h,storage=base_upload_api
    h.setup_engine=installation.engine
    def sessions(request: Request):
        with Session(get_engine(),expire_on_commit=False) as db:
            yield db
    app.dependency_overrides[get_db]=sessions
    try:yield h,storage
    finally:app.dependency_overrides.pop(get_db,None)
