"""Role tool schemas and prompts, including the explicit legacy plan contract."""
from __future__ import annotations
from copy import deepcopy
import re
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator
from semora_agent.runtime import AgentSubmission
from semora_agent.intent_rules import REQUEST_INTERPRETATION_RULES, SOURCE_PLANNING_RULES
from semora_contracts import (QueryOrder, ReviewFinding, ReviewResolution, ClarificationQuestion,
                             QueryCalculation, QueryFilter, QueryStage, SourceQueryStep)

class AnalysisPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator('source_steps','stages',mode='before',check_fields=False)
    @classmethod
    def normalize_unused_step_arrays(cls,value,info):
        if not isinstance(value,list):return value
        normalized=deepcopy(value)
        def stages(items):
            if not isinstance(items,list):return
            for stage in items:
                if not isinstance(stage,dict):continue
                if stage.get('filters','absent') is None:stage['filters']=[]
                measures=stage.get('measures')
                if not isinstance(measures,list):continue
                for measure in measures:
                    if isinstance(measure,dict) and measure.get('filters','absent') is None:measure['filters']=[]
        if info.field_name=='stages':
            stages(normalized)
            return normalized
        for step in normalized:
            if not isinstance(step,dict):continue
            query=step.get('query')
            if not isinstance(query,dict):continue
            for name in ('fields','metrics','dimensions','filters','calculations','stages','relationship_path','order_by','assumptions','evidence'):
                if name in query and query[name] is None:query[name]=[]
            stages(query.get('stages'))
        return normalized
    order_by: list[QueryOrder] | None = Field(default=None, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)
    summary: str | None = None
    intent: str | None = None
    subject: str | None = None
    metrics: list[str] | None = Field(default=None, description='最终输出指标name，只能引用已发布指标或本次payload.calculations[].name；stages[].measures只是中间指标，不能直接当作最终指标。')
    dimensions: list[str] | None = None
    fields: list[str] | None = None
    filters: list[QueryFilter] | None = None
    having: list[QueryFilter] | None = None
    optional_relationships: list[str] | None = None
    time_range: str | None = None
    grain: str | None = Field(default=None, description='本体对象的name，必须来自当前objects列表；不能填写字段、中文标签或阶段名。最终分组字段另填dimensions。')
    relationship_path: list[str] | None = None
    calculations: list[QueryCalculation] | None = Field(default=None, description='所有未发布的最终指标必须在这里完整定义name/function/field。校验时必须完整携带；最终使用candidate_id引用已校验方案，无需重复生成。')
    stages: list[QueryStage] | None = Field(default=None, max_length=8)
    source_steps: list[SourceQueryStep] | None = Field(default=None, max_length=8)
    aggregation_rules: list[str] | None = None
    output_fields: list[str] | None = None
    assumptions: list[str] | None = None
    confidence: dict[str, Annotated[float, Field(ge=0, le=1)]] | None = Field(default=None, description='关键字段到0至1数值置信度的映射；无法评估时为空对象。不能填写说明文字。')
    analysis_steps: list[str] | None = None
    clarifications: list[ClarificationQuestion] | None = None


class BoundedReviewFinding(ReviewFinding):
    message: str = Field(min_length=1, max_length=600,description='影响结果的具体问题。')
    evidence: str = Field(default='', max_length=600,description='当前资料或产物中的可核验依据。')
    acceptance: str = Field(min_length=1, max_length=600,description='问题解决后应满足的具体条件。')


class ReviewPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    findings: list[BoundedReviewFinding] = Field(default_factory=list, max_length=8,description='具体问题对象放在此数组，包含位置、依据和验收条件；无问题为[]。')
    resolutions: list[ReviewResolution] = Field(default_factory=list, max_length=24)
    decision: Literal['approved', 'revise', 'rejected'] = Field(description='一致且无实质问题为approved；可修复为revise；不允许的操作为rejected。')
    issues: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=8,description='问题摘要字符串数组；具体问题对象写findings。')
    rationale: str = Field(min_length=1, max_length=1200)


class InitialReviewPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approved', 'revise', 'rejected'] = Field(description='一致且无实质问题为approved；可修复为revise；不允许的操作为rejected。')
    issues: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=8,description='问题摘要字符串数组；具体问题对象写findings。')
    rationale: str = Field(min_length=1, max_length=1200)
    findings: list[BoundedReviewFinding] = Field(default_factory=list, max_length=8,description='具体问题对象放在此数组，包含位置、依据和验收条件；无问题为[]。')


def review_submission(pending=None,repair_paths=None):
    """An initial review cannot resolve findings that do not exist."""
    finding=BoundedReviewFinding
    if repair_paths:
        finding=create_model('CurrentReviewFinding',__base__=BoundedReviewFinding,
            paths=(list[Literal[tuple(repair_paths)]],Field(default_factory=list,max_length=12,description='从当前可修改字段的JSON Pointer枚举中选择。')))
    if not pending:
        payload=create_model('InitialScopedReviewPayload',__base__=InitialReviewPayload,
            findings=(list[finding],Field(default_factory=list,max_length=8))) if repair_paths else InitialReviewPayload
        return create_model('AgentSubmission', __base__=AgentSubmission, payload=(payload, ...))
    known=tuple(f['id'] for f in pending)
    resolution=create_model('CurrentResolution',__base__=ReviewResolution,
        finding_id=(Literal[known],...),
        rationale=(str,Field(min_length=1,max_length=600)))
    payload = create_model('CurrentReviewPayload', __base__=ReviewPayload,
        findings=(list[finding],Field(default_factory=list,max_length=8)),
        resolutions=(list[resolution], Field(..., min_length=sum(bool(f.get('structured')) for f in pending), max_length=len(pending))))
    return create_model('AgentSubmission', __base__=AgentSubmission, payload=(payload, ...))


class CompactDelivery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str | None = Field(default=None,description='已校验候选引用；采用候选时仅填此字段。')
    summary: str | None = None
    clarifications: list[ClarificationQuestion] | None = Field(default=None,max_length=3,description='影响当前结果且尚未确定的业务问题。')
    blocked_reason: str | None = Field(default=None,min_length=1,max_length=1000)


class AnalysisDelivery(AnalysisPayload):
    candidate_id: str | None = Field(default=None, description='validate_plan返回的候选方案ID。引用时只填此字段，不重复或覆盖方案；澄清时改填clarifications。')
    blocked_reason: str | None = Field(default=None,min_length=1,max_length=1000)


def require_plan_controls(schema):
    """Every single-source step must decide ordering and business Top N too."""
    if isinstance(schema,dict):
        properties=schema.get('properties',{})
        if {'order_by','limit'} <= properties.keys():
            schema['required']=list(dict.fromkeys([*schema.get('required',[]),'order_by','limit']))
            properties['order_by']['description']='必须显式填写本次执行排序；无排序用[]，不能只写入analysis_steps。'
            properties['limit']['description']='必须显式填写本次取前N条的N；不设业务TopN用null。本轮修改优先于历史数量。'
        for key in ('fields','dimensions'):
            prop=properties.get(key)
            if isinstance(prop,dict):
                prop['description']='只填完整对象.属性或阶段.字段；计算指标名必须放metrics，不能放在此数组。'
        for child in schema.values():require_plan_controls(child)
    elif isinstance(schema,list):
        for child in schema:require_plan_controls(child)


def constrain_source_ids(schema,source_ids):
    if isinstance(schema,dict):
        prop=schema.get('properties',{}).get('data_source_id')
        if isinstance(prop,dict):
            prop['enum']=source_ids
            prop['description']='必须逐字复制当前授权数据源ID，不能省略UUID段或自行缩写。'
        for child in schema.values():constrain_source_ids(child,source_ids)
    elif isinstance(schema,list):
        for child in schema:constrain_source_ids(child,source_ids)


class CandidateDiagnostics(ValueError):
    def __init__(self,issues):
        self.issues=issues
        super().__init__('；'.join(i['path']+': '+i['message'] for i in issues))


class PlanPatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path:str=Field(min_length=1,max_length=200,description='替换路径，支持source_steps.2.query.stages、source_steps[2].query.stages或/source_steps/2/query/stages。')
    value_json:str=Field(max_length=8000,description='替换值的JSON，例如空数组写[]，字符串要带JSON双引号。')

    @field_validator('path',mode='before')
    @classmethod
    def normalize_path(cls,value):
        if not isinstance(value,str):return value
        path=re.sub(r'\([^)]*\)','',value.strip())
        path=re.sub(r'\[(\d+)\]',r'.\1',path.lstrip('/').replace('/','.')).removeprefix('analysis.')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*',path):raise ValueError('补丁路径必须使用点路径、数字下标或JSON Pointer')
        return path


# Keep the same terminal tool name across roles, with explicit per-role schemas.
AnalysisSubmission = create_model('AgentSubmission', __base__=AgentSubmission, payload=(AnalysisDelivery, ...))
ReviewSubmission = create_model('AgentSubmission', __base__=AgentSubmission, payload=(ReviewPayload, ...))


from semora_agent.legacy_prompts import ANALYST_PROMPT, REVIEWER_PROMPT
