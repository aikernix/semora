"""Small ontology observations for LLMs; published definitions remain unchanged."""
from __future__ import annotations

import copy
import json


SEMANTIC_KEYS = (
    'name', 'label', 'table', 'column', 'type', 'primaryKey', 'nullable',
    'sensitive', 'sensitivity', 'unit', 'aliases', 'enum', 'enumValues',
    'valueDefinitions', 'defaultFilters', 'filters', 'expression', 'grain',
    'from', 'to', 'condition', 'cardinality', 'joinType', 'optional',
    'description', 'evidenceStatus', 'dataSourceId', 'projection',
)


def compact_definition(value):
    """Keep execution and business meaning, excluding repeated import workbooks."""
    metadata = value.get('metadata') or {}
    result = {key: value[key] if key in value else metadata[key]
              for key in SEMANTIC_KEYS if key in value or key in metadata}
    if 'properties' in value:
        result['properties'] = [compact_definition(prop) for prop in value['properties']]
    return result


def compact_observation(value):
    result = dict(value)
    if isinstance(result.get('object'), dict):
        result['object'] = compact_definition(result['object'])
    for key in ('relations', 'metrics'):
        if isinstance(result.get(key), list):
            result[key] = [compact_definition(item) for item in result[key]]
    if 'object' in result:
        # Hub objects can have hundreds of relationships. Provide every edge
        # as an index; a chosen path returns the complete join definitions.
        result['relations'] = [{key: relation[key] for key in ('name', 'label', 'from', 'to', 'cardinality') if key in relation}
                               for relation in result.get('relations', [])]
        result['relation_details'] = '以上是完整直接关联目录；使用find_relation_path读取目标路径的完整condition及业务角色说明。'
    return result


def compact_memory(memory):
    """Also shrink legacy observations on a follow-up without mutating audit data."""
    result = copy.deepcopy(memory)
    for message in result:
        data = message.get('data', {})
        if message.get('type') != 'tool' or data.get('name') not in {'read_object', 'find_relation_path'}:
            continue
        try:
            observation = json.loads(data.get('content', ''))
        except (TypeError, ValueError):
            continue
        if isinstance(observation, dict):
            data['content'] = json.dumps(compact_observation(observation), ensure_ascii=False)
    return result
