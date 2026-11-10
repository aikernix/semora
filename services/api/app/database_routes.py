"""Database extension APIs. Existing source IDs and synchronous scan remain compatible."""
import asyncio
from datetime import datetime, timezone
import io
import json
import zipfile
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel, Field
from semora_contracts.database import PRODUCTS
from .datasources import datasource_service
from .mdl_export import build_mdl, merge_snapshot

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
            item.scan_result.pop("capabilities",None);item.scan_result["scan_stale"]=True;item.status="unverified"
            datasource_service.repository.save(item)
    return result

@router.get("/datasources/{identifier}/capabilities")
def capabilities(identifier: str):
    item=source(identifier)
    return {"connection":item.scan_result.get("connection",{}),
            "capabilities":item.public()["capabilities"] or {k:"pending" for k in ("connection","structure","comments","constraints","mdl","query")}}

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

@router.get("/datasources/{identifier}/relationships")
def relations(identifier: str):
    result=source(identifier).scan_result
    return {"declared":result.get("foreign_keys",[]),"candidates":result.get("relationship_candidates",[]),"decisions":result.get("relationship_decisions",{})}

class Decision(BaseModel):
    status: str=Field(pattern=r"^(accepted|rejected|pending)$")
    note: str=Field(default="",max_length=2000)

@router.put("/datasources/{identifier}/relationships/{candidate_id}")
def decide(identifier: str,candidate_id: str,body: Decision):
    item=source(identifier)
    candidate=next((r for r in item.scan_result.get("relationship_candidates",[]) if r["id"]==candidate_id),None)
    if not candidate:raise HTTPException(404,"关系候选不存在")
    candidate["status"]=body.status
    item.scan_result.setdefault("relationship_decisions",{})[candidate_id]={**body.model_dump(),"updated_at":datetime.now(timezone.utc).isoformat()}
    datasource_service.repository.save(item)
    return candidate

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
async def validate_query(identifier: str):
    item=source(identifier)
    try:result=await executor_request("POST","datasources/validate-query",json=payload(item))
    except HTTPException:
        item.status="unverified"
        item.scan_result.setdefault("capabilities",{})["query"]="failed"
        datasource_service.repository.save(item)
        raise
    datasource_service.repository.record_test(identifier,result)
    return result


@router.post("/datasources/{identifier}/relationships/{candidate_id}/profile")
async def profile(identifier: str,candidate_id: str):
    item=source(identifier)
    candidate=next((r for r in item.scan_result.get("relationship_candidates",[]) if r["id"]==candidate_id),None)
    if not candidate:raise HTTPException(404,"候选不存在")
    result=await executor_request("POST","datasources/profile-relationship",json={"datasource":payload(item),
        "source_table":candidate["source_table"],"source_columns":candidate["columns"],
        "target_table":candidate["target_table"],"target_columns":candidate["target_columns"],"sample_rows":200})
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
