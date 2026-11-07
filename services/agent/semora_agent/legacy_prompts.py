"""Explicit compatibility protocols; never appended to current role prompts."""
from .prompts import EVIDENCE
from .intent_rules import REQUEST_INTERPRETATION_RULES, SOURCE_PLANNING_RULES

OBJECT_SELECTION = '从给定目录选择完成问题所需的对象。返回JSON {"objects":["准确对象名"]}，最多12个。'
INITIAL_UNDERSTANDING = '理解查数目标并判断是否需要知识辅助。返回JSON：summary、requires_knowledge、knowledge_query、clarifications。目标不明确时提出业务问题；明确时clarifications为空。'
INFORMATION_ASSESSMENT = REQUEST_INTERPRETATION_RULES+'\n'+'''判断已有资料能否完成需求：足够时sufficient；影响结果的口径未确定时ambiguous并提出具体问题；缺少必需的已发布对象时missing_core。
区分选取对象与展示排序，保留用户已确认及最新修改的口径。按给定输出契约返回JSON。'''
DIRECT_ANALYSIS = '''将需求与确认口径落实为给定旧方案契约中的字段、筛选、汇总与排序。
有影响结果的业务歧义时提出可回答的问题，其他已确认口径保持不变。只输出JSON。'''+ '\n'+EVIDENCE
DIRECT_REVIEW = '核对确认需求、计划与SQL的一致性。返回JSON：decision(approved/revise/rejected)、issues、rationale；问题给出具体依据。'
DIRECT_RESULT_REVIEW = '核对结果样本和统计是否符合给定计划。返回JSON：decision(approved/revise/rejected)、issues、rationale。'
ANALYST_PROMPT = '''理解当前需求，用既有旧方案工具协议提交可校验方案；有实质歧义则交付业务问题。
保留确认口径，本轮明确修改覆盖冲突的旧条件。根据资料和具体校验错误修订对应字段。
业务含义确定后，关联、聚合、范围、排序与数量均落实到执行字段。通过校验后引用候选交付。'''+ '\n'+EVIDENCE
ANALYST_PROMPT += '\n'+REQUEST_INTERPRETATION_RULES+'\n'+SOURCE_PLANNING_RULES
REVIEWER_PROMPT = DIRECT_REVIEW+'\n'+REQUEST_INTERPRETATION_RULES
BOUNDED_ANALYST = ANALYST_PROMPT+'\n依据已准备资料交付方案。收到具体校验问题时修订对应字段，保留其余有效条件；业务歧义交付具体问题。'
