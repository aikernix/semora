import pytest
from fastapi import HTTPException
from semora_contracts import SQLArtifact
from app.planner import adapt_mysql_artifact
from services.executor.app.main import assert_readonly
from services.executor.app.mysql_adapter import begin_readonly


def test_mysql_compilation_preserves_bindings_and_translates_dates():
    artifact = SQLArtifact(
        semantic_sql='SELECT balance FROM loan',
        physical_sql="SELECT ROUND(SUM(l.balance)::numeric, 2) AS balance FROM public.loans l WHERE l.branch_id IN (%(branch_id_0)s) AND l.as_of_date >= CURRENT_DATE - INTERVAL '3 months' LIMIT 1000",
        parameters={'branch_id_0': 2}, artifact_hash='original',
    )
    result = adapt_mysql_artifact(artifact)
    assert result.dialect == 'mysql'
    assert '%(branch_id_0)s' in result.physical_sql
    assert 'public.' not in result.physical_sql
    assert 'MONTH' in result.physical_sql.upper()
    assert 'DECIMAL(38, 10)' in result.physical_sql.upper()
    assert result.parameters == artifact.parameters
    assert result.artifact_hash != artifact.artifact_hash
    assert_readonly(result.physical_sql, 'mysql')


@pytest.mark.parametrize('sql', [
    'SELECT 1; DROP TABLE loans',
    "SELECT * FROM loans INTO OUTFILE '/tmp/export' LIMIT 1",
    'SELECT * FROM loans LIMIT 1 FOR UPDATE',
    'SELECT SLEEP(20) LIMIT 1',
])
def test_mysql_blocks_writes_locks_exports_and_side_effects(sql):
    with pytest.raises(HTTPException):
        assert_readonly(sql, 'mysql')


def test_mysql_readonly_transaction_sets_server_timeout_first():
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): calls.append(args)
    class Connection:
        def cursor(self): return Cursor()
    begin_readonly(Connection(), 3000)
    assert calls == [
        ('SET SESSION MAX_EXECUTION_TIME = %s', (3000,)),
        ('START TRANSACTION READ ONLY',),
    ]
