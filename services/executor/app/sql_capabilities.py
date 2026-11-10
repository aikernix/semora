"""Bounded read-only probes, run on explicit source validation, never per question."""
import psycopg
from semora_contracts.database import SQL_BASELINE_ID
from . import mysql_adapter

def probe_native(kind, dsn):
    if kind not in {"mysql","postgresql"}:
        raise ValueError("native capability probe requires an explicit adapter")
    queries={
        "cte":"WITH semora_probe AS (SELECT 1 AS n) SELECT n FROM semora_probe",
        "window_functions":"SELECT ROW_NUMBER() OVER (ORDER BY n) FROM (SELECT 1 AS n) semora_probe",
        "recursive_cte":"WITH RECURSIVE semora_probe(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM semora_probe WHERE n<2) SELECT MAX(n) FROM semora_probe",
        "pagination":"SELECT 1 AS n LIMIT 1",
        "offset_pagination":"SELECT n FROM (SELECT 1 AS n UNION ALL SELECT 2 AS n) semora_probe ORDER BY n LIMIT 1 OFFSET 1",
    }
    factory=(lambda:mysql_adapter.connect(dsn,3000)) if kind=="mysql" else (lambda:psycopg.connect(dsn,connect_timeout=5))
    probes={}
    with factory() as connection:
        for feature,sql in queries.items():
            try:
                if kind=="mysql":mysql_adapter.begin_readonly(connection,3000)
                with connection.cursor() as cursor:
                    if kind=="postgresql":
                        cursor.execute("SET TRANSACTION READ ONLY")
                        cursor.execute("SET LOCAL statement_timeout='3s'")
                    cursor.execute(sql)
                    row=cursor.fetchone()
                    expected=2 if feature in {"recursive_cte","offset_pagination"} else 1
                    if not row or row[0]!=expected:raise ValueError("unexpected capability probe result")
                probes[feature]={"status":"verified","method":"synthetic_readonly_probe"}
            except Exception as exc:
                # Failure means unavailable, not proof of a missing SQL feature.
                code=getattr(exc,"sqlstate",None)
                if code is None and exc.args and isinstance(exc.args[0],int):code=exc.args[0]
                probes[feature]={"status":"unavailable","code":code}
            finally:connection.rollback()
    return {"sql_baseline":SQL_BASELINE_ID,**probes}
