"""Targeted, local review evidence; never opens general retrieval."""
from hashlib import sha256
import json


def supplement_review(toolkit, objects, purpose, reason, field_refs, acceptance):
    packet = toolkit.context_state['packet']
    selected = set(packet['selected_objects'])
    if not set(objects) <= selected:
        return {'available': False, 'finalize': True,
                'message': '申请对象不属于当前方案；请依据已有资料提交复核结论。'}
    if not reason.strip() or not acceptance.strip() or not field_refs:
        return {'available': False, 'message': '请明确缺失的field_refs、影响当前条件的reason及补齐后核验的acceptance。'}
    rows = [row for row in packet['objects'] if row['object']['name'] in objects]
    available = {ref for row in rows for ref in row['field_refs']}
    if not set(field_refs) <= available:
        return {'available': False, 'finalize': True,
                'missing_fields': sorted(set(field_refs) - available),
                'message': '字段未在当前相关本体中声明；记录具体配置缺口，不继续扩展检索。'}
    # Ignore cosmetic changes in purpose/reason when recognizing repeated gaps.
    signature = sha256(json.dumps({'binding': toolkit.context_state['binding'],
                                  'fields': sorted(set(field_refs))}, sort_keys=True).encode()).hexdigest()
    seen = toolkit.run.agent_memory.setdefault('review_context_reads', [])
    if signature in seen:
        return {'available': True, 'unchanged': True, 'finalize': True,
                'message': '相同字段资料已提供；使用已有证据提交结论，不重复读取。'}
    seen.append(signature)
    return {'available': True, 'finalize': True, 'objects': rows,
            'relations': [r for r in packet.get('relations', [])
                          if r['from'] in objects and r['to'] in objects],
            'field_refs': sorted(set(field_refs)),
            'message': '已返回指定字段所在对象的完整定义及相关关系；现在提交复核结论。'}
