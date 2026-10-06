"""Legacy resource-writer setup for original read regressions.

Accounts and GETs keep runtime auth. Unconverted review/publication writer
HTTP is used only to prepare resources using the isolated privileged engine;
no principal, auth predicate, receipt, journal or runtime grant is replaced.
These assertions do not certify those writers as runtime-ready.
"""
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
        resource_writer=(request.method=='POST' and (
            (request.url.path.startswith('/admin/review-requests/') and request.url.path.endswith(('/approve','/reject'))) or
            (request.url.path.startswith('/review-requests/') and request.url.path.endswith('/withdraw')) or
            (request.url.path.startswith('/admin/common-copies/') and request.url.path.endswith(('/publish','/revoke')))))
        with Session(installation.engine if resource_writer else get_engine(),expire_on_commit=False) as db:
            yield db
    app.dependency_overrides[get_db]=sessions
    try:yield h,storage
    finally:app.dependency_overrides.pop(get_db,None)
