"""Origin policy independently of pending; no recovery/auth predicate patch."""
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import HttpSecurity, normalise_origin, origin_allowed, get_settings
from app.api.csrf import install_csrf_middleware
from app.api.deps import require_trusted_origin
from app.api.errors import install_error_handlers


@pytest.mark.parametrize('value',['http://example.test/path','http://example.test/','http://u:p@example.test',
    'http://example.test?x','http://example.test#x','http://*.example.test','http://example.test:0',
    'http://example.test:65536','http://example.test:','http://example.test:bad','https://',
    'null','ftp://example.test','http://example.test.evil/','http://example.test\\evil',
    'http://exa mple.test','http://example.test%00','http://-bad.test','http://bad..test'])
def test_invalid_config_origin(value):
    with pytest.raises(ValidationError):HttpSecurity(allowed_origins=[value])


def test_origin_normalisation_exact_scheme_host_effective_port():
    s=HttpSecurity(allowed_origins=['HTTPS://EXAMPLE.TEST:443','https://example.test','http://[::1]:80'])
    assert s.allowed_origins==['https://example.test','http://[::1]']
    assert origin_allowed('https://example.test:443',s.allowed_origins)
    for v in (None,'null','http://example.test','https://example.test:444','https://example.test.evil','https://evil-example.test'):
        assert not origin_allowed(v,s.allowed_origins)
    assert not origin_allowed('http://localhost:3000',[])


@pytest.fixture
def origin_client(monkeypatch):
    for key in ('HOST','PORT','USER','PASSWORD','NAME'):
        monkeypatch.delenv('RAS_DB__'+key,raising=False)
    monkeypatch.setenv('RAS_DATABASE_URL','postgresql+psycopg://origin:synthetic@127.0.0.1:58435/postgres')
    monkeypatch.setenv('RAS_HTTP__ALLOWED_ORIGINS','["http://web.test"]')
    get_settings.cache_clear()
    app=FastAPI();install_error_handlers(app);install_csrf_middleware(app)
    @app.api_route('/write',methods=['POST','PUT','PATCH','DELETE'])
    def write():return {'written':True}
    @app.post('/dependency',dependencies=[Depends(require_trusted_origin)])
    def dependency():return {'written':True}
    @app.api_route('/safe',methods=['GET','HEAD','OPTIONS'])
    def safe():return {'ok':True}
    with TestClient(app) as client:
        client.cookies.set('ras_csrf','synthetic-csrf')
        yield client
    get_settings.cache_clear()


@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE'])
@pytest.mark.parametrize('origin',[None,'null','http://evil.test','https://web.test','http://web.test:3000',
                                 'http://web.test.evil','http://evil-web.test','http://web.test/path','http://web.test'])
def test_all_write_methods_explicit_origin(origin_client,method,origin):
    headers={'x-csrf-token':'synthetic-csrf','referer':'http://web.test','x-forwarded-host':'web.test','host':'web.test'}
    if origin is not None:headers['origin']=origin
    response=origin_client.request(method,'/write',headers=headers)
    assert response.status_code==(200 if origin=='http://web.test' else 403)


@pytest.mark.parametrize('token',[None,'wrong'])
def test_allowed_origin_still_requires_csrf(origin_client,token):
    headers={'origin':'http://web.test'}
    if token:headers['x-csrf-token']=token
    assert origin_client.post('/write',headers=headers).status_code==403


def test_safe_methods_and_duplicate_origin(origin_client):
    for method in ['GET','HEAD','OPTIONS']:assert origin_client.request(method,'/safe').status_code==200
    assert origin_client.post('/write',headers=[('origin','http://web.test'),('origin','http://web.test'),
                                              ('x-csrf-token','synthetic-csrf')]).status_code==403


def test_dependency_same_rule(origin_client):
    assert origin_client.post('/dependency',headers={'origin':'http://web.test','x-csrf-token':'synthetic-csrf'}).status_code==200
    assert origin_client.post('/dependency',headers={'x-csrf-token':'synthetic-csrf'}).status_code==403


@pytest.mark.parametrize('origin',[None,'null','https://web.test','http://web.test:81','http://web.test.evil','http://web.test'])
def test_dependency_without_middleware(origin):
    from starlette.requests import Request
    from types import SimpleNamespace
    from app.api.errors import ApiError
    headers=[] if origin is None else [(b'origin',origin.encode())]
    request=Request({'type':'http','method':'POST','headers':headers})
    settings=SimpleNamespace(http=HttpSecurity(allowed_origins=['http://web.test']))
    if origin=='http://web.test':require_trusted_origin(request,settings)
    else:
        with pytest.raises(ApiError,match='csrf_failed'):require_trusted_origin(request,settings)
