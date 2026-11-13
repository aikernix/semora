"""Database extension APIs. Existing source IDs and synchronous scan remain compatible."""
import asyncio
from datetime import datetime, timezone
import io
import json
import zipfile
from uuid import uuid4
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from semora_contracts.database import PRODUCTS
from .datasources import datasource_service
from .mdl_export import build_mdl, merge_snapshot, column_type
from .datasource_table_storage import tables_digest
from .ontology import ontology_service
from .datasource_modeling import ModelingService, TERMINAL, _indexed

router=APIRouter(prefix="/api/v1")
TASKS={}

def source(identifier):
    try:return datasource_service.repository.get(identifier)
    except KeyError:raise HTTPException(404,"数据源不存在") from None

async def executor_request(method, path, **kwargs):
    executor=datasource_service.executor
    try:
        async with httpx.AsyncClient(timeout=200) as client:
            response=await client.request(method,executor.base_url+"/internal/v1/"+path,
                headers={"X-Semora-Internal-Token":executor.token},**kwargs)
            if not response.is_success:
                try:detail=response.json().get("detail","Executor 请求失败")
                except ValueError:detail="Executor 请求失败"
                raise HTTPException(response.status_code,detail)
            return response.json()
    except httpx.HTTPError as exc:raise HTTPException(502,"Executor 请求失败："+type(exc).__name__) from exc

def payload(item):
    return {"datasource_id":item.id,"type":item.type,"secret_ref":item.secret_ref}

modeling_service=ModelingService(datasource_service,executor_request,ontology_service=ontology_service)

@router.get("/datasource-products")
def products():return [{"id":k,**v,"validation":"unverified"} for k,v in PRODUCTS.items()]

@router.get("/settings/database-drivers")
async def drivers():return await executor_request("GET","database-drivers")

@router.post("/settings/database-drivers",status_code=201)
async def install_driver(config: str=Form(...),files: list[UploadFile]=File(...)):
    data=[];total=0
    for file in files:
        content=await file.read(100*1024**2+1);total+=len(content)
        if total>100*1024**2:raise HTTPException(413,"驱动包超过 100MB")
        data.append(("files",(file.filename,content,"application/java-archive")))
    return await executor_request("POST","database-drivers",data={"config":config},files=data)

@router.put("/settings/database-drivers/{driver_id}")
async def update_driver(driver_id: str,body: dict):
    result=await executor_request("PUT",f"database-drivers/{driver_id}",json=body)
    # Connections referencing this immutable bundle need fresh validation.
    for item in datasource_service.repository.list():
        if item.type not in {"mysql","postgresql"}:
            item.scan_result.pop("capability_cache", None)
            item.scan_result.get("connection", {}).pop("probes", None)
            item.scan_result.pop("capabilities",None);item.scan_result["scan_stale"]=True;item.status="unverified"
            datasource_service.repository.save(item)
    return result

@router.get("/datasources/{identifier}/capabilities")
def capabilities(identifier: str):
    item=source(identifier)
    from .database_profile import source_profile
    profile=source_profile(item)
    return {"connection":item.scan_result.get("connection",{}),
            "capabilities":item.public()["capabilities"] or {k:"pending" for k in ("connection","structure","comments","constraints","mdl","query")},
            "probe_cache":item.scan_result.get("connection",{}).get("probe_cache",{}),
            "features":profile["features"],"function_capabilities":profile["function_capabilities"]}

@router.get("/datasources/{identifier}/snapshot")
def snapshot(identifier: str):return source(identifier).scan_result

class ScanRequest(BaseModel):
    tables: list[str] | None=Field(default=None,min_length=1,max_length=1000)

@router.post("/datasources/{identifier}/scan-jobs",status_code=202)
async def start_scan(identifier: str,body: ScanRequest):
    item=source(identifier)
    old=item.scan_result.get("scan_job",{})
    if len(TASKS)>=2:raise HTTPException(429,"同时最多运行两个结构扫描")
    if old.get("id") in TASKS:raise HTTPException(409,"已有扫描任务正在运行")
    job={"id":uuid4().hex,"status":"running","phase":"metadata","completed_tables":0,
         "started_at":datetime.now(timezone.utc).isoformat()}
    item.scan_result={**item.scan_result,"scan_job":job};datasource_service.repository.save(item)
    async def work():
        try:
            result=await executor_request("POST","datasources/scan",json={**payload(item),"tables":body.tables,"execution_id":job["id"]})
            if job["status"]!="running":return
            # A connection edit invalidates the old task before it can publish stale metadata.
            if source(identifier).scan_result.get("scan_job",{}).get("id")!=job["id"]:return
            job.update(status="completed",phase="complete",completed_tables=len(result.get("tables",[])))
            result["scan_job"]=job
            datasource_service.repository.record_scan(identifier,result)
            modeling_service.start(identifier)
        except asyncio.CancelledError:job.update(status="cancelled",phase="cancelled")
        except Exception as exc:
            job.update(status="failed",phase="failed",error=exc.detail if isinstance(exc,HTTPException) else type(exc).__name__)
        finally:
            if source(identifier).scan_result.get("scan_job",{}).get("id")==job["id"]:
                source(identifier).scan_result["scan_job"]=job;datasource_service.repository.save(source(identifier))
            TASKS.pop(job["id"],None)
    TASKS[job["id"]]=asyncio.create_task(work())
    return job

@router.get("/datasources/{identifier}/scan-jobs/{job_id}")
async def scan_status(identifier: str,job_id: str):
    item=source(identifier);job=item.scan_result.get("scan_job",{})
    if job.get("id")!=job_id:raise HTTPException(404,"扫描任务不存在")
    if job.get("status")=="running" and job_id not in TASKS:
        job.update(status="interrupted",phase="interrupted");datasource_service.repository.save(item)
    if job.get("status")=="running":
        progress=await executor_request("GET",f"executions/{job_id}/progress")
        job.update(progress)
    return job

@router.post("/datasources/{identifier}/scan-jobs/{job_id}/cancel")
async def cancel_scan(identifier: str,job_id: str):
    job=await scan_status(identifier,job_id)
    if job.get("status")=="running":
        job.update(status="cancelled",phase="cancelled")
        await executor_request("POST",f"executions/{job_id}/cancel")
        task=TASKS.get(job_id)
        if task:task.cancel()
        datasource_service.repository.save(source(identifier))
    return job

class Decision(BaseModel):
    status: str=Field(pattern=r"^(accepted|rejected|pending)$")
    note: str=Field(default="",max_length=2000)


@router.get("/datasources/{identifier}/tables")
def datasource_tables(
    identifier: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    query: str = Query(default="", max_length=200),
):
    source(identifier)
    return datasource_service.repository.table_page(identifier, query, page, page_size)


@router.get("/datasources/{identifier}/tables/detail")
def datasource_table_detail(identifier: str, table_id: str = Query(min_length=1, max_length=512)):
    try:
        return datasource_service.repository.table_detail(identifier, table_id)
    except KeyError:
        raise HTTPException(404, "数据表不存在") from None


class RelationshipKeyPair(BaseModel):
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)


class RelationshipEdit(BaseModel):
    source_table_id: str = Field(min_length=1, max_length=512)
    target_table_id: str = Field(min_length=1, max_length=512)
    key_pairs: list[RelationshipKeyPair] = Field(min_length=1, max_length=8)
    cardinality: Literal["1:1", "1:N", "N:1", "N:N"]


def _relationship_view(candidate):
    grade = candidate.get("evidence_grade")
    origin = "foreign_key" if grade == "foreign_key" else "manual" if grade == "manual" else "inferred"
    return {
        "id": candidate["id"],
        "source_table_id": candidate["source_table_id"],
        "target_table_id": candidate["target_table_id"],
        "source_columns": candidate["source_columns"],
        "target_columns": candidate["target_columns"],
        "key_pairs": candidate["key_pairs"],
        "cardinality": candidate["cardinality"],
        "status": candidate["status"],
        "origin": origin,
        "evidence_grade": grade,
        "confidence": float(candidate.get("confidence") or 0),
    }


@router.get("/datasources/{identifier}/relationships")
def relations(
    identifier: str,
    status: str = Query(default="", pattern=r"^(|accepted|pending|rejected|stale)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    source(identifier)
    values = modeling_service.repository.list_candidates(identifier, "single_source")
    if status:
        values = [item for item in values if item.get("status") == status]
    values.sort(key=lambda item: ({"accepted": 0, "pending": 1, "rejected": 2, "stale": 3}.get(item.get("status"), 4), -float(item.get("confidence") or 0), item["id"]))
    total = len(values)
    window = values[(page - 1) * page_size:page * page_size]
    return {"items": [_relationship_view(item) for item in window], "page": page, "page_size": page_size, "total": total}


def _type_family(column):
    value = column_type(column)
    if not value:
        return ""
    if value.startswith("decimal") or value in {"smallint", "integer", "bigint", "real", "double"}:
        return "number"
    if value in {"varchar", "char", "uuid"}:
        return "text"
    if value in {"date", "time", "timestamp", "timestamptz"}:
        return "temporal"
    return value


def _validated_manual_candidate(identifier: str, body: RelationshipEdit):
    item = source(identifier)
    tables = {table.get("id"): table for table in item.scan_result.get("tables") or []}
    left, right = tables.get(body.source_table_id), tables.get(body.target_table_id)
    if not left or not right:
        raise HTTPException(422, "关联关系引用了未扫描的数据表")
    left_columns = {column.get("name"): column for column in left.get("columns") or []}
    right_columns = {column.get("name"): column for column in right.get("columns") or []}
    pairs = [pair.model_dump() for pair in body.key_pairs]
    if len({(pair["source"], pair["target"]) for pair in pairs}) != len(pairs):
        raise HTTPException(422, "关联字段对不能重复")
    for pair in pairs:
        source_column, target_column = left_columns.get(pair["source"]), right_columns.get(pair["target"])
        if not source_column or not target_column:
            raise HTTPException(422, "关联关系引用了不存在的字段")
        if not _type_family(source_column) or _type_family(source_column) != _type_family(target_column):
            raise HTTPException(422, "关联字段类型不兼容")
    schema_hash = tables_digest(list(tables.values()))
    try:
        package = modeling_service.repository.get_package(identifier)
    except KeyError:
        raise HTTPException(409, "单源本体尚未生成完成") from None
    if package["schema_hash"] != schema_hash or package["status"] not in {"ready", "ready_with_pending"}:
        raise HTTPException(409, "单源本体正在更新，请完成后再编辑关系")
    return item, pairs, schema_hash


@router.put("/datasources/{identifier}/relationships/{candidate_id}")
async def edit_relationship(identifier: str, candidate_id: str, body: RelationshipEdit):
    try:
        previous = modeling_service.repository.get_candidate(candidate_id)
    except KeyError:
        raise HTTPException(404, "关联关系不存在") from None
    if previous["scope"] != "single_source" or previous["source_datasource_id"] != identifier:
        raise HTTPException(404, "关联关系不存在")
    _, pairs, schema_hash = _validated_manual_candidate(identifier, body)
    modeling_service.repository.decide_candidate(candidate_id, "rejected", "data-steward", "已由人工配置替代")
    manual_id = "manual_" + uuid4().hex
    candidate = {
        "id": manual_id,
        "scope": "single_source",
        "source_datasource_id": identifier,
        "target_datasource_id": identifier,
        "source_table_id": body.source_table_id,
        "target_table_id": body.target_table_id,
        "source_columns": [pair["source"] for pair in pairs],
        "target_columns": [pair["target"] for pair in pairs],
        "key_pairs": pairs,
        "condition_template": {"operator": "and_equals", "pairs": [{"left": pair["source"], "right": pair["target"]} for pair in pairs]},
        "evidence": [{"kind": "manual_configuration", "replaces": candidate_id}],
        "evidence_grade": "manual",
        "confidence": 1.0,
        "cardinality": body.cardinality,
        "status": "accepted",
        "domain": None,
    }
    modeling_service.repository.replace_candidates([candidate], schema_hash, schema_hash)
    job = modeling_service.start_package_rebuild(identifier)
    return {
        **_relationship_view(modeling_service.repository.get_candidate(manual_id)),
        "modeling_job_id": job["id"],
    }


@router.delete("/datasources/{identifier}/relationships/{candidate_id}")
async def delete_relationship(identifier: str, candidate_id: str):
    try:
        candidate = modeling_service.repository.get_candidate(candidate_id)
    except KeyError:
        raise HTTPException(404, "关联关系不存在") from None
    if candidate["scope"] != "single_source" or candidate["source_datasource_id"] != identifier:
        raise HTTPException(404, "关联关系不存在")
    try:
        package = modeling_service.repository.get_package(identifier)
    except KeyError:
        raise HTTPException(409, "单源本体尚未生成完成") from None
    if package["schema_hash"] != tables_digest(source(identifier).scan_result.get("tables") or []):
        raise HTTPException(409, "单源本体正在更新，请完成后再停用关系")
    candidate = modeling_service.repository.decide_candidate(
        candidate_id, "rejected", "data-steward", "人工停用"
    )
    job = modeling_service.start_package_rebuild(identifier)
    return {**_relationship_view(candidate), "modeling_job_id": job["id"]}


@router.post("/datasources/{identifier}/relationships/{candidate_id}/restore")
async def restore_relationship(identifier: str, candidate_id: str):
    item = source(identifier)
    try:
        candidate = modeling_service.repository.get_candidate(candidate_id)
        package = modeling_service.repository.get_package(identifier)
    except KeyError:
        raise HTTPException(404, "关联关系不存在") from None
    if candidate["scope"] != "single_source" or candidate["source_datasource_id"] != identifier:
        raise HTTPException(404, "关联关系不存在")
    current_hash = tables_digest(item.scan_result.get("tables") or [])
    if package["schema_hash"] != current_hash or candidate.get("source_schema_hash") != current_hash:
        raise HTTPException(409, "数据结构已变化，不能恢复旧关系；请重新配置")
    candidate = modeling_service.repository.decide_candidate(
        candidate_id, "accepted", "data-steward", "人工恢复"
    )
    job = modeling_service.start_package_rebuild(identifier)
    return {**_relationship_view(candidate), "modeling_job_id": job["id"]}

@router.get("/datasources/{identifier}/mdl-report")
def mdl_report(identifier: str):
    item=source(identifier);_,report=build_mdl(item.scan_result,item.type)
    return report

@router.get("/datasources/{identifier}/mdl")
def download_mdl(identifier: str):
    item=source(identifier);mdl,report=build_mdl(item.scan_result,item.type)
    output=io.BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        if mdl is not None:archive.writestr("mdl.json",json.dumps(mdl,ensure_ascii=False,sort_keys=True,indent=2))
        archive.writestr("report.json",json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2))
    item.scan_result.setdefault("capabilities",{})["mdl"]="verified" if report["status"]=="passed" else "partial" if mdl else "failed"
    datasource_service.repository.save(item)
    return Response(output.getvalue(),media_type="application/zip",headers={"Content-Disposition":'attachment; filename="semora-mdl.zip"'})


@router.post("/datasources/{identifier}/validate-query")
async def validate_query(identifier: str, force: bool = False):
    item=source(identifier)
    from .capability_service import validate_cached, TASKS as probe_tasks
    try:
        pending = probe_tasks.get(identifier)
        result = await asyncio.shield(pending) if pending else None
        if result is None or force:
            result=await validate_cached(item, datasource_service.repository, executor_request, force=force)
    except HTTPException:
        item.status="unverified"
        item.scan_result.setdefault("capabilities",{})["query"]="failed"
        datasource_service.repository.save(item)
        raise
    return result


@router.post("/datasources/{identifier}/relationships/{candidate_id}/profile")
async def profile(identifier: str,candidate_id: str):
    item=source(identifier)
    candidate=next((r for r in item.scan_result.get("relationship_candidates",[]) if r["id"]==candidate_id),None)
    if not candidate:raise HTTPException(404,"候选不存在")
    table_map={table.get("id"):table for table in item.scan_result.get("tables",[])}
    source_table=table_map.get(candidate["source_table"],{})
    target_table=table_map.get(candidate["target_table"],{})
    if not _indexed(source_table,candidate["columns"]) or not _indexed(target_table,candidate["target_columns"],unique=True):
        raise HTTPException(422,"该候选没有满足安全采样要求的来源索引和目标唯一索引")
    result=await executor_request("POST","datasources/profile-relationship",json={"datasource":payload(item),
        "source_table":candidate["source_table"],"source_columns":candidate["columns"],
        "target_table":candidate["target_table"],"target_columns":candidate["target_columns"],"sample_rows":200,
        "source_index_confirmed":True,"target_unique_index_confirmed":True})
    candidate["profile"]=result;datasource_service.repository.save(item)
    return result


class OntologyRelationInput(BaseModel):
    domain: str=Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    source_object: str=Field(min_length=1,max_length=200)
    target_object: str=Field(min_length=1,max_length=200)

@router.post("/datasources/{identifier}/relationships/{candidate_id}/ontology-candidate")
async def ontology_candidate(identifier: str,candidate_id: str,body: OntologyRelationInput):
    from .ontology import ontology_service,OntologyCoreError
    from .mdl_export import quoted
    item=source(identifier)
    candidate=next((r for r in item.scan_result.get("relationship_candidates",[]) if r["id"]==candidate_id),None)
    if not candidate:raise HTTPException(404,"候选不存在")
    try:
        draft=await ontology_service.get_configuration(body.domain)
        objects={o["name"]:o for o in draft.get("objects",[])}
        left,right=objects.get(body.source_object),objects.get(body.target_object)
        if not left or not right:raise HTTPException(422,"请先在本体维护中建立两端对象")
        for obj,table in [(left,candidate["source_table"]),(right,candidate["target_table"])]:
            sid=obj.get("dataSourceId") or draft.get("dataSourceId")
            if sid!=identifier or obj.get("table")!=table:raise HTTPException(422,"对象物理表或数据源与候选不一致")
        def fields(obj, columns):
            mapping={p.get("column"):p["name"] for p in obj.get("properties",[])}
            if any(c not in mapping for c in columns):raise HTTPException(422,"请先补齐本体字段映射")
            return [mapping[c] for c in columns]
        a,b=fields(left,candidate["columns"]),fields(right,candidate["target_columns"])
        condition=" AND ".join(f"{quoted(left['name'])}.{quoted(x)} = {quoted(right['name'])}.{quoted(y)}" for x,y in zip(a,b))
        # This creates a review candidate; publishing stays with existing ontology maintenance.
        return await ontology_service.create_candidate({"domain":body.domain,"term":"r_"+candidate_id,
            "source":"datasource:"+identifier,"proposal":"结构推断关系，需人工确认业务含义","kind":"relation",
            "patch":{"name":"r_"+candidate_id,"from":left["name"],"to":right["name"],"condition":condition,"cardinality":"N:1","joinType":"many_to_one"},
            "evidence":{"locator":"schema-scan","excerpt":"；".join(candidate["evidence"])}})
    except OntologyCoreError as exc:raise HTTPException(exc.status_code,str(exc)) from exc


@router.get("/datasources/{identifier}/onboarding")
def onboarding_status(identifier: str):
    source(identifier)
    job=modeling_service.repository.latest_job(identifier)
    if not job:raise HTTPException(404,"数据源尚未开始自动建模")
    return job


@router.post("/datasources/{identifier}/onboarding/retry",status_code=202)
async def retry_onboarding(identifier: str):
    source(identifier)
    return modeling_service.start(identifier,force=False)


@router.post("/datasources/{identifier}/rescan",status_code=202)
async def rescan(identifier: str):
    source(identifier)
    return modeling_service.start(identifier,force=True)


@router.get("/datasources/{identifier}/source-ontology")
def source_ontology(identifier: str):
    source(identifier)
    try:package=modeling_service.repository.get_package(identifier)
    except KeyError:raise HTTPException(404,"数据源尚未生成单源本体包") from None
    package["candidates"]=modeling_service.repository.list_candidates(identifier,"single_source")
    return package


@router.put("/relationship-candidates/{candidate_id}")
async def decide_discovered_candidate(candidate_id: str, body: Decision):
    try:
        return await modeling_service.decide_candidate(
            candidate_id, body.status, reviewer="data-steward", note=body.note
        )
    except KeyError:
        raise HTTPException(404,"关系候选不存在") from None
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@router.post("/ontology/configurations/{domain}/sources/{identifier}")
async def bind_source_package(domain: str,identifier: str):
    item=source(identifier)
    try:package=modeling_service.repository.get_package(identifier)
    except KeyError:raise HTTPException(409,"请先完成数据源自动建模") from None
    from .datasource_table_storage import tables_digest
    tables=item.scan_result.get("tables") or []
    if (item.status!="healthy" or not tables or package["schema_hash"]!=tables_digest(tables)
            or package["status"] not in {"ready","ready_with_pending"}):
        raise HTTPException(409,"数据源自动建模尚未完成或结构已过期")
    try:configuration=await ontology_service.get_configuration(domain)
    except Exception as exc:raise HTTPException(404,"业务域不存在") from exc
    objects={obj["name"]:obj for obj in configuration.get("objects",[])}
    for obj in package["payload"].get("objects",[]):objects[obj["name"]]=obj
    relations={relation["name"]:relation for relation in configuration.get("relations",[])}
    for relation in package["payload"].get("relations",[]):
        relation={**relation,"metadata":{**relation.get("metadata",{}),"sourcePackageId":identifier}}
        relations[relation["name"]]=relation
    refs={ref["dataSourceId"]:ref for ref in configuration.get("sourcePackageRefs",[]) if ref.get("dataSourceId")}
    refs[identifier]={"dataSourceId":identifier,"schemaHash":package["schema_hash"],"version":package["generator_version"]}
    configuration.update(objects=list(objects.values()),relations=list(relations.values()),sourcePackageRefs=list(refs.values()))
    configuration["dataSourceId"]=configuration.get("dataSourceId") or item.id
    return await ontology_service.save_configuration(domain,configuration)


class CrossSourceInput(BaseModel):
    left_source_id: str=Field(min_length=1,max_length=64)
    right_source_id: str=Field(min_length=1,max_length=64)


@router.post("/ontology/configurations/{domain}/cross-source-analysis",status_code=202)
async def start_cross_source(domain: str,body: CrossSourceInput):
    try:configuration=await ontology_service.get_configuration(domain)
    except Exception as exc:raise HTTPException(404,"业务域不存在") from exc
    bound={ref.get("dataSourceId") for ref in configuration.get("sourcePackageRefs",[])}
    bound.update(obj.get("dataSourceId") or configuration.get("dataSourceId") for obj in configuration.get("objects",[]))
    if body.left_source_id not in bound or body.right_source_id not in bound:
        raise HTTPException(422,"两个数据源必须先绑定到当前业务域")
    try:return modeling_service.start_cross_source(domain,body.left_source_id,body.right_source_id)
    except (KeyError,ValueError) as exc:raise HTTPException(409,str(exc)) from exc


@router.get("/ontology/configurations/{domain}/cross-source-analysis/{job_id}")
def cross_source_status(domain: str,job_id: str):
    try:job=modeling_service.repository.get_job(job_id)
    except KeyError:raise HTTPException(404,"跨源分析任务不存在") from None
    if job.get("domain")!=domain or job.get("kind")!="cross_source":raise HTTPException(404,"跨源分析任务不存在")
    if job["status"] in TERMINAL:
        job["candidates"]=modeling_service.repository.list_cross_candidates(
            job["datasource_id"],job["peer_datasource_id"],domain
        )
    return job
