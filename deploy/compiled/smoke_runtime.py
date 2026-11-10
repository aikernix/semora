"""Exercise native imports, request signatures, and SQL functionality without credentials."""
import asyncio
import importlib
import importlib.machinery
import inspect
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient


service = sys.argv[1]
manifest = json.loads(Path('/app/compiled-manifest.json').read_text())
for name in manifest['modules']:
    module = importlib.import_module(name)
    assert isinstance(module.__loader__, importlib.machinery.ExtensionFileLoader), name

from app.main import app

with TestClient(app) as client:
    assert client.get('/healthz').status_code == 200
    schema = client.get('/openapi.json')
    assert schema.status_code == 200
    assert schema.json()['paths']
    if service == 'api':
        assert client.get('/api/v1/model/status').status_code == 200
        # Wrong parameter types must still be caught after native compilation.
        assert client.post('/api/v1/sessions', json={'title': '', 'domain': 'test'}).status_code == 422
        from app.main import list_domains
        assert inspect.iscoroutinefunction(list_domains)
        try:
            inspect.getsource(list_domains)
        except (OSError, TypeError):
            pass
        else:
            raise AssertionError('Original endpoint source is readable')
        from app.result_engine import local_query
        from semora_contracts import SQLProgramStep
        value = SQLProgramStep(name='check', purpose='Check', data_source_id='source',
                               sql='SELECT id FROM items', output_columns=['id'])
        assert value.model_dump()['output_columns'] == ['id']
        assert callable(local_query)
    else:
        from app.main import assert_readonly
        assert_readonly('SELECT 1', 'postgres')
        try:
            assert_readonly('DELETE FROM items', 'postgres')
        except Exception:
            pass
        else:
            raise AssertionError('Write query accepted')

print(f'{service}: native imports, HTTP routing, OpenAPI and contracts passed')
