"""Schemas generated from the same contracts that validate model responses."""
import json
from .relationship_contracts import DiscoveryOutput, ReadOutput, CompactResearchOutput, prompt_schema

DISCOVERY_SCHEMA=prompt_schema(DiscoveryOutput)
READ_SCHEMA=prompt_schema(ReadOutput)
RESEARCH_SCHEMA=prompt_schema(CompactResearchOutput)
MARKER='\n\n【输出格式要求】\n'


def output_format(schema):
    return MARKER+'仅返回符合下列结构定义的JSON对象。\n'+json.dumps(schema,ensure_ascii=False,separators=(',',':'))
