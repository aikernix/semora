"""Actionable catalog diagnostics; suggestions never authorize a field."""
from __future__ import annotations

from difflib import get_close_matches


def field_error(references, objects):
    catalog = {}
    object_aliases = {}
    for obj, value in objects.items():
        object_aliases[obj] = {obj.casefold(), str(value.get('label') or '').casefold(), str(value.get('table') or '').split('.')[-1].casefold()}
        for prop in value.get('properties', []):
            ref = f"{obj}.{prop['name']}"
            catalog[ref] = {str(prop.get(key) or '').casefold() for key in ('name', 'column', 'label')}
    invalid = [(path, ref) for path, ref in references if ref not in catalog]
    if not invalid:
        return None
    parts = []
    for path, ref in invalid[:8]:
        obj, _, term = ref.rpartition('.')
        def matches_object(name):
            return not obj or obj.casefold() in object_aliases[name.split('.', 1)[0]]
        candidates = [name for name, aliases in catalog.items()
                      if matches_object(name)
                      and (term or ref).casefold() in aliases]
        if not candidates:
            pool = [name for name in catalog if matches_object(name)]
            candidates = get_close_matches(ref, pool, n=3, cutoff=0.45)
        parts.append(f"{path}={ref}" + (f"；候选字段：{', '.join(candidates[:3])}" if candidates else '；请调用 read_object 查看该对象的 field_refs'))
    return ('模型选择了未发布字段：' + '；'.join(parts)
            + '。必须使用本体对象名.属性name，不能使用中文标签、物理表名或column。候选仅供核验，不会自动映射；找不到所需字段应提出澄清。')
