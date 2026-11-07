"""Interpretation rules retained only for explicit compatibility protocols."""

REQUEST_INTERPRETATION_RULES = """本轮明确修改覆盖冲突的旧口径，保留其他确认内容。区分选取集合与展示排序、不同实体与事件记录。只对会改变结果的未确定业务含义提问，问题面向业务用户并允许自由补充。"""

SOURCE_PLANNING_RULES = """每步query引用其数据源的对象和本地关系；inputs按已发布边界关系传递完整key_pairs，引用真实前步输出。source_layout和transfer_templates提供可用映射。同源依赖步骤和目标源内继续关联均有效。输出补充来源字段需保留目标关联键；需要预聚合时按源对象完整关联键形成唯一分组，再关联并汇总。"""
