"""Small JSON output contracts for calls that do not use tool schemas."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class SQLExplanation(BaseModel):
    explanation: str = Field(min_length=1)


class AnswerExplanation(BaseModel):
    answer: str = Field(min_length=1)


class CandidateProposal(BaseModel):
    term: str = Field(min_length=1)
    proposal: str = Field(min_length=1)
    kind: Literal['definition','metric','object','property','relation']
    patch: dict[str,Any] = Field(description='definition为空；metric包含name、label、expression、grain、dimensions、format，引用当前对象属性；其他映射须原文明确支持。')
    evidence_chunk_id: str = Field(description='支撑候选的给定分块id。')


class CandidateBatch(BaseModel):
    candidates: list[CandidateProposal]


class KnowledgeAction(BaseModel):
    model_config=ConfigDict(extra='ignore')
    action: Literal['wiki_search','wiki_read_page','read_source','finish']
    kb_id: str = Field(default='',description='给定知识库准确ID；仅一个库时可省略。')
    query: str = Field(default='',description='搜索动作的一个短关键词或别名。')
    slug: str = Field(default='',description='观察中已发现的页面标识。')
    document_id: str = Field(default='',description='已读内容引用的原文标识。')
    page: int = Field(default=1,ge=1)
    summary: str = Field(default='',description='finish时提交已取得事实、冲突与未知项。')


def knowledge_action_schema(*, must_finish=False):
    schema=KnowledgeAction.model_json_schema()
    if must_finish:
        schema['properties']['action']['enum']=['finish']
    return schema
