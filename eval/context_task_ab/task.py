"""IS1公开任务合同；不包含推荐答案。"""
PROMPT = '''请为RIDGE-31比较四家供应商方案，按采购资料中的当前范围、两年总成本和硬性要求给出推荐与备选。自行查阅本地buyer/及vendors/资料，注意修订、必需附加费用和完整交付条件。按buyer/deliverables.md生成outputs/comparison.json与outputs/recommendation.md，列出可定位的原文证据和待核验事项。原始资料只读；不要采购、部署或访问网络。'''
TASK_ID = 'IS1'
CONTRACT = dict(task_id=TASK_ID, domain='procurement', category='multi_file_information_synthesis',
    user_prompt=PROMPT, fixture='vendor-review', allowed_changes=['outputs/**'],
    forbidden_changes=['buyer/**','vendors/**','README.md'],
    expected_artifacts=['outputs/comparison.json','outputs/recommendation.md'],
    initial_session='EMPTY', history_generation='AUTONOMOUS_TOOL_USE',
    mechanism='PRODUCTION_V2_COMPACTION_ON_VS_EVAL_OFF', evidence='OFFLINE_GATE_ONLY')
