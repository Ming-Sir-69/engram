import asyncio
import sqlite3

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engram.mcp.oauth import OwnerOAuth


def test_independent_service_scope_and_metadata(tmp_path):
    p=OwnerOAuth(tmp_path/'shadow','https://example.test/shadow-memory',scope='shadow:observe',resource_name='Shadow Memory Lab')
    q=OwnerOAuth(tmp_path/'engram','https://example.test/engram')
    with TestClient(Starlette(routes=p.routes())) as c:
        r=c.get('/.well-known/oauth-protected-resource').json()
    assert r['scopes_supported']==['shadow:observe']
    assert r['resource_name']=='Shadow Memory Lab'
    assert p.consent_path=='/shadow-memory/consent/'
    with p.connect() as c: token=p.issue(c,'test-client',['shadow:observe'])
    assert asyncio.run(p.load_access_token(token.access_token)) is not None
    assert asyncio.run(q.load_access_token(token.access_token)) is None
    assert q.scope=='engram:access'


def test_oauth_database_connection_closes_after_operation(tmp_path):
    p=OwnerOAuth(tmp_path/'auth','https://example.test/engram')
    with p.connect() as c:
        c.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute('SELECT 1')
