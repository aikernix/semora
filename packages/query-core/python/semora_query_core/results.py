"""Bounded deterministic merging of complete aggregate partials; no model/IO."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
from functools import cmp_to_key

from .sources import output_alias, property_definition, type_family


def membership_result(step,source,matched,*,max_rows,ontology):
    spec=step.membership
    if source.truncated or matched.truncated:
        raise ValueError('存在性筛选关联集合不完整，不能把未查全的数据认定为不存在')
    binding=step.inputs[0]
    def indices(result,refs):
        aliases=[output_alias(f) for f in refs]
        if not set(aliases)<=set(result.columns) or set(aliases)&set(result.summary.get('masked_columns',[])):
            raise ValueError('存在性筛选缺少真实字段或关联字段已脱敏')
        return [result.columns.index(a) for a in aliases]
    sk=indices(source,[p.source_field for p in binding.pairs])
    tk=indices(matched,[p.target_field for p in binding.pairs])
    gi=indices(source,spec.group_by);fi=indices(source,spec.fields)
    families=[type_family(property_definition(p.source_field,ontology)[1]['type']) for p in binding.pairs]
    def key(row,indices):
        parts=[]
        for i,family in zip(indices,families):
            value=row[i]
            if value is None:return None
            if family=='number':
                value=Decimal(str(value))
                if not value.is_finite():raise ValueError('存在性关联键数值无效')
            elif family=='integer' and (not isinstance(value,int) or isinstance(value,bool)):
                raise ValueError('存在性关联键类型与本体不一致')
            elif family=='text' and not isinstance(value,str):
                raise ValueError('存在性关联键类型与本体不一致')
            parts.append(value)
        return tuple(parts)
    source_keys={key(row,sk) for row in source.rows}-{None}
    matches={key(row,tk) for row in matched.rows}-{None}
    if matches-source_keys:
        raise ValueError('存在性返回键无法精确匹配来源，不能猜测数据库排序规则或判定不存在')
    # Exact values, including SQL NULL semantics. Cross-source type/collation
    # compatibility is checked before lookup; never treat NULL=NULL as a match.
    present_groups={tuple(row[i] for i in gi) for row in source.rows
        if key(row,sk) in matches}
    rows=[];seen=set()
    for row in source.rows:
        group=tuple(row[i] for i in gi)
        if (group in present_groups)!=spec.present:continue
        values=tuple(row[i] for i in fi)
        if values not in seen:seen.add(values);rows.append(list(values))
        if len(rows)>max_rows:raise ValueError('存在性筛选输出超过行数限制')
    columns=[output_alias(f) for f in spec.fields]
    def compare(a,b):
        for order in spec.order_by:
            i=columns.index(output_alias(order.field));x,y=a[i],b[i]
            if x is None or y is None:diff=0 if x is y else (1 if x is None else -1)
            else:diff=((x>y)-(x<y))*(-1 if order.direction=='desc' else 1)
            if diff:return diff
        return 0
    rows.sort(key=cmp_to_key(compare))
    if spec.limit is not None:rows=rows[:spec.limit]
    result=matched.model_copy(deep=True)
    result.columns,result.rows,result.row_count=columns,rows,len(rows)
    result.summary.update({'masked_columns':[],'membership':{'present':spec.present,'source_rows':source.row_count,
        'matched_rows':matched.row_count,'group_by':spec.group_by,'complete':True,'null_keys':'never_match'}})
    return result


def regroup_result(step, result, *, max_rows, ontology):
    spec=step.regroup
    if spec is None:
        return result
    if result.truncated:
        raise ValueError('跨源再汇总的局部结果不完整，禁止交付部分总计')
    binding=step.inputs[0]
    def column(ref):
        return binding.source_step+'__'+output_alias(ref) if ref in binding.include_source_fields else output_alias(ref)
    group_columns=[column(f) for f in spec.group_by]
    names=[m.name for m in spec.measures]
    required=group_columns+names
    if not set(required)<=set(result.columns):
        raise ValueError('再汇总缺少约定的维度或指标列')
    if set(required)&set(result.summary.get('masked_columns',[])):
        raise ValueError('再汇总字段已脱敏，禁止将脱敏值合并成业务总计')
    gi=[result.columns.index(c) for c in group_columns]
    mi=[result.columns.index(c) for c in names]
    numeric={m.name for m in step.query.calculations if m.function in {'sum','count'} or
             type_family(property_definition(m.field,ontology)[1]['type']) in {'integer','number'}}
    grouped={}
    for row in result.rows:
        key=tuple(row[i] for i in gi)
        values=grouped.setdefault(key,[None]*len(mi))
        for j,(index,measure) in enumerate(zip(mi,spec.measures)):
            value=row[index]
            if value is None:
                continue
            if measure.name in numeric:
                value=Decimal(str(value))
                if not value.is_finite():
                    raise ValueError('再汇总指标包含非有限数值')
            if measure.function in {'sum','count'}:
                if isinstance(value,bool):
                    raise ValueError('再汇总指标必须为数值')
                number=Decimal(str(value))
                if not number.is_finite():
                    raise ValueError('再汇总指标包含非有限数值')
                previous=values[j] or Decimal(0)
                # Preserve DB NUMERIC precision instead of silently using the
                # process-wide Decimal default (28 significant digits).
                a,b=previous.as_tuple(),number.as_tuple()
                with localcontext() as context:
                    context.prec=max(28,max(len(a.digits)+a.exponent,len(b.digits)+b.exponent)-min(a.exponent,b.exponent)+2)
                    values[j]=previous+number
            else:
                values[j]=value if values[j] is None else (min(values[j],value) if measure.function=='min' else max(values[j],value))
        if len(grouped)>max_rows:
            raise ValueError('再汇总结果超过当前行数限制')
    columns=[output_alias(f) for f in spec.group_by]+names
    rows=[list(key)+values for key,values in grouped.items()]
    # Sort exact partial totals, then format only once. Never sum rounded values.
    def compare(a,b):
        for order in spec.order_by:
            i=columns.index(output_alias(order.field))
            x,y=a[i],b[i]
            if x is None or y is None:
                diff=0 if x is y else (1 if x is None else -1)  # NULLS LAST
            else:
                diff=(x>y)-(x<y)
                if order.direction=='desc': diff=-diff
            if diff:return diff
        return 0
    rows.sort(key=cmp_to_key(compare))
    if spec.limit is not None: rows=rows[:spec.limit]
    for row in rows:
        for j,measure in enumerate(spec.measures,len(gi)):
            if isinstance(row[j],(Decimal,int,float)) and not isinstance(row[j],bool):
                number=Decimal(str(row[j]))
                with localcontext() as context:
                    context.prec=max(28,len(number.as_tuple().digits)+max(number.as_tuple().exponent,0)+3)
                    row[j]=number.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
    merged=result.model_copy(deep=True)
    merged.columns,merged.rows,merged.row_count=columns,rows,len(rows)
    merged.summary.update({'regroup':{'group_by':spec.group_by,'measures':[m.model_dump(mode='json') for m in spec.measures],
        'partial_rows':result.row_count,'complete':True,'ordering':'after_regroup','rounding':'once_after_merge'},'masked_columns':[]})
    return merged
