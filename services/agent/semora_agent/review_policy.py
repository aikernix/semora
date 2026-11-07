"""Bounded, domain-independent repair protocol for query-plan reviews."""
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import re

from semora_contracts import ClarificationQuestion, QueryCalculation


class ReviewTarget(StrEnum):
    BUSINESS = 'business'
    ARTIFACT = 'artifact'


@dataclass(frozen=True)
class ReviewIssue:
    """Role-independent, immutable interpretation of one review finding."""
    id: str
    kind: str
    target: ReviewTarget
    paths: tuple[str, ...]
    message: str
    evidence: str
    acceptance: str

    @classmethod
    def from_finding(cls, finding):
        targets = {ReviewTarget.BUSINESS if p.startswith('/query_intent/') else ReviewTarget.ARTIFACT
                   for p in finding.paths}
        if finding.kind == 'repair' and len(targets) > 1:
            raise ValueError('A repair issue must not mix business intent and executable artifact paths')
        target = next(iter(targets), ReviewTarget.ARTIFACT)
        return cls(finding.id, finding.kind, target, tuple(finding.paths),
                   finding.message, finding.evidence, finding.acceptance)


REVIEW_PROTOCOL = """
按execution_capabilities复核，不要求Analyst生成契约无法表达的功能，不允许用其他指标替代用户目标。
问题必须写入findings，每项含kind、paths、message、evidence、acceptance；issues保留简短展示。
kind=repair：当前能力可实现的字段、筛选或计算错误。paths使用当前plan可定位的JSON Pointer，指向应修改的执行字段或集合，例如/filters或/source_steps/0/query/calculations，不指向说明文字。
kind=clarification：用户口径冲突或证据不足以确定业务含义，question写一个用户可理解的问题；不得替用户选择。
kind=capability：execution_capabilities明确不支持的必要运算或后处理，capability_key必须引用unsupported中的键，说明所缺能力并给出question询问是否调整查询范围；不要反复要求修订无法表达的方案。
每个问题给出可核验的acceptance，不能只说“优化方案”。同一问题沿用pending_findings中的id。
收到pending_findings时，resolutions逐项填finding_id、resolved/unresolved、rationale。只能依据执行字段和证据标记resolved，改标签或assumptions不算解决。尚未解决不能approved。
已解决的问题不要无新证据再次提出；新增问题说明新的证据。approved时findings为空，pending_findings必须全部resolved。
"""


def capabilities():
    return {
        'aggregate_functions': QueryCalculation.model_json_schema()['properties']['function']['enum'],
        'supported': ['已发布字段与指标', '参数化筛选', '分组聚合', '单源预聚合', '跨源关联键传递', '跨源完整局部结果按唯一键映射的业务维度再次汇总SUM/COUNT/MIN/MAX，合并后排序和限量', '输出字段或指标排序order_by', '明确排序后取前N条limit（1至1000，可在每个跨源步骤独立设置）'],
        'unsupported': {
            'ad_hoc_scalar_expression': '临时方案不能声明CHAR_LENGTH等标量函数或任意表达式；已发布指标仍可按其定义使用',
            'ai_row_analysis': '当前没有逐条文本评价、评分、分类或其他AI后处理执行步骤',
              'extremal_record_attributes': '没有通用的按最大、最小或最新记录取其其他属性的执行算子；分别MAX两个字段不能保证来自同一条记录。需要已发布的相应指标或明确列入未完成目标，不能用独立聚合冒充。',
        },
        'rule': '不能用另一字段或指标替代缺失能力；不能把无法执行的条件只写进说明。',
        'relationship_existence': 'existence指定已发布关系、anchor及present；支持同源/跨源明细存在性或反存在性筛选，完整键集合按主体分组核验；不支持与聚合混用。',
    }


def execution_view(plan):
    value = plan.model_dump(mode='json') if hasattr(plan, 'model_dump') else deepcopy(plan)
    descriptive = {'intent', 'summary', 'label', 'assumptions', 'evidence', 'time_range', 'analysis_steps'}
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items() if k not in descriptive}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v
    return clean(value)


_MISSING = object()
ARTIFACT_ROOTS={'metrics','calculations','dimensions','fields','filters','grain','relationship_path','stages','source_steps','order_by','limit','having','optional_relationships'}
BUSINESS_ROOTS={'subject','fields','group_by','metrics','measures','filters','relationship_roles','selection','record_scope','order_by','limit','existence','having','optional_relationships'}


def review_target_view(plan):
    view=execution_view(plan)
    if view.get('query_intent'):
        intent=view['query_intent']
        intent.setdefault('having',[]);intent.setdefault('optional_relationships',[])
        intent.setdefault('existence',None)
        for measure in intent.get('measures',[]):
            measure.setdefault('filters',[]);measure.setdefault('null_value',None)
    return view


def repairable_paths(plan,*,business_only=False):
    view=review_target_view(plan)
    paths=[]
    def collect(value,path):
        paths.append(path)
        if path.count('/')>=4:return
        if isinstance(value,dict):
            for key,child in value.items():collect(child,path+'/'+str(key).replace('~','~0').replace('/','~1'))
        elif isinstance(value,list):
            for index,child in enumerate(value):collect(child,path+'/'+str(index))
    for root in sorted(BUSINESS_ROOTS):
        if root in (view.get('query_intent') or {}):collect(view['query_intent'][root],'/query_intent/'+root)
    if not business_only:
        for root in sorted(ARTIFACT_ROOTS):
            if root in view:collect(view[root],'/'+root)
    return paths

def pointer(value, path):
    if not path.startswith('/'):
        return _MISSING
    try:
        for part in path[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not re.fullmatch(r'0|[1-9][0-9]*', part):
                    return _MISSING
                value = value[int(part)]
            else:
                value = value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return _MISSING


def issue_ledger(check, previous):
    prior = {f['id']: f for f in previous}
    pending = {r.finding_id for r in check.resolutions if r.status == 'unresolved'}
    entries = {key: deepcopy(prior[key]) for key in pending if key in prior}
    for f in check.findings:
        item = f.model_dump(mode='json')
        signature = json.dumps([f.kind, f.paths, f.message, f.acceptance], ensure_ascii=False)
        item['id'] = f.id if f.id in prior else 'issue_' + hashlib.sha256(signature.encode()).hexdigest()[:12]
        item['structured'] = True
        entries[item['id']] = item
    if not check.findings and not entries and check.decision != 'approved':
        # Legacy clients still receive a bounded full-execution comparison.
        for message in check.issues or [check.rationale]:
            key = 'issue_' + hashlib.sha256(message.encode()).hexdigest()[:12]
            entries[key] = {'id': key, 'kind': 'repair', 'paths': [], 'message': message,
                            'acceptance': message, 'structured': False}
    return list(entries.values())


def check_revision(before, after, pending):
    old, new = execution_view(before), execution_view(after)
    unchanged = []
    for finding in pending:
        if finding['kind'] != 'repair':
            continue
        paths = finding.get('paths') or []
        if not paths:
            same = old == new
        else:
            # Every declared repair target must change; semantic correctness is
            # independently checked by Reviewer, never inferred from a diff.
            same = any(pointer(old, p) == pointer(new, p) for p in paths)
        if same:
            unchanged.append(finding['id'])
    return unchanged


def validate_resolution(check, pending, plan=None):
    ids = [r.finding_id for r in check.resolutions]
    expected = {f['id'] for f in pending if f.get('structured')}
    known = {f['id'] for f in pending}
    if len(ids) != len(set(ids)) or set(ids) - known or expected - set(ids):
        raise ValueError('resolutions必须逐项覆盖待复核问题ID，不得遗漏、重复或编造；必须覆盖：'+','.join(sorted(expected))+'；遗漏：'+','.join(sorted(expected-set(ids))))
    if check.decision == 'approved' and (check.findings or any(r.status != 'resolved' for r in check.resolutions)):
        raise ValueError('仍有未解决问题，不能approved')
    for finding in check.findings:
        if finding.kind == 'capability' and finding.capability_key not in capabilities()['unsupported']:
            raise ValueError('capability_key必须引用execution_capabilities.unsupported中的已知能力边界')
    if plan is not None:
        view = review_target_view(plan)
        for finding in check.findings:
            issue = ReviewIssue.from_finding(finding)
            roots = ARTIFACT_ROOTS
            business_roots = BUSINESS_ROOTS
            def valid_path(path):
                parts=path.split('/')
                if len(parts)<2 or not path.startswith('/'):
                    return False
                if issue.target == ReviewTarget.BUSINESS:
                    if parts[1] != 'query_intent':return False
                    return bool(view.get('query_intent')) and len(parts)>2 and parts[2] in business_roots and (pointer(view,path) is not _MISSING or (path=='/query_intent/existence'))
                return parts[1] in roots and pointer(view,path) is not _MISSING
            invalid = any(not valid_path(p) for p in finding.paths)
            if finding.kind == 'repair' and (not finding.paths or invalid):
                bad=[p for p in finding.paths if not valid_path(p)]
                raise ValueError('repair.paths无效：'+json.dumps(bad,ensure_ascii=False)+'；只能定位当前业务意图或执行字段，不得引用说明文字。业务字段使用/query_intent/前缀。')


def route_findings(check, outcome):
    blocked = [f for f in check.findings if f.kind in {'clarification', 'capability'}]
    if not blocked:
        return None
    questions = []
    for f in blocked[:3]:
        key = hashlib.sha256((f.kind + f.message).encode()).hexdigest()[:12]
        questions.append(ClarificationQuestion(
            id='review_' + key, field=f.kind,
            question=f.question or ('当前执行能力无法完成这一部分需求，是否调整本次查询范围？' if f.kind == 'capability' else '请补充或确认这部分业务口径。'),
            reason=f.message, options=[], allow_free_text=True, blocking=True))
    requirement = outcome.requirement.model_copy(deep=True, update={
        'status': 'awaiting_clarification', 'clarifications': questions})
    return replace(outcome, plan=None, requirement=requirement, clarification_question=questions[0].question)


def intent_repair_route(decision):
    """Typed policy, independent of Workflow, Agent runtime and compiler."""
    if any(f.kind in {'clarification', 'capability'} for f in decision.findings):
        return 'clarification'
    issues = [ReviewIssue.from_finding(f) for f in decision.findings]
    if issues and all(i.paths and i.target == ReviewTarget.BUSINESS for i in issues):
        return 'business'
    return 'technical'


__all__ = ['ReviewIssue','ReviewTarget','REVIEW_PROTOCOL','capabilities','execution_view',
           'pointer','issue_ledger','check_revision','validate_resolution','route_findings','intent_repair_route']
