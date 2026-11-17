"""Developer control case; never merged into historical Run G evidence."""
from planning.scoring_evidence import artifact_evidence_index, judge_scoring_row


def exit_control(contract):
    bindings = contract.get('source_obligations') or []
    deadline = bindings[0]['text'] if bindings else '按合同约定的退出时限'
    steps = [
        '项目退出移交由项目负责人牵头。收到合同终止或提前解除的书面通知后，编制退出计划与退出流程，确认移交双方、接续岗位和清点范围。准备期间继续履行原服务义务。',
        f'人员撤场与自有资产撤出按来源要求执行：{deadline}，将人员和自有资产撤出物业服务区域。接续人员先完成岗位交底，值守与服务安排衔接后分批撤离。',
        '资产设备按权属核对设备清单。甲方办公、服务用房逐处点交，自有设备按合同撤出；设备移交记录区分产权与现场状态，由对应责任岗位核验。',
        '资料移交按目录逐卷核对全部物业管理档案和物业服务资料，包括已形成的运行记录、维修记录、工程设备档案。缺失资料列入问题清单，记录补齐责任岗位与处理情况。',
        '物业管理权按约移交。钥匙与通行授权按实际清单核对；如存在经甲方授权使用的系统账号，则逐项办理系统权限交回，由甲方确认接收。',
        '未完事项、遗留问题和责任分歧分别登记在问题清单，注明处置岗位、当前状态和双方商定的后续接口。局部争议保留书面记录，按甲方确认口径处理。',
        '过渡安排以服务连续性为检查对象。交接前后值守、巡视、保洁和食堂作业按既定要求执行，交接班记录与接续岗位名单对应，避免因人员撤场产生服务空档。',
        '双方确认逐项核对用房、设备、资料、管理权与未完事项，验收签字形成移交确认清单，双方留存签认件。系统权限交回、资料移交等存在差异时在清单中登记，不将口头确认视为完成。',
    ]
    fragment = {'section_id': 'CONTROL-EXIT', 'content_unit_fragments': [
        {'content_unit_id': 'CONTROL-EXIT-CU', 'archetype': 'SOP', 'content': '\n'.join(steps),
         'payload': {'title': '项目退出移交流程', 'steps': steps}}]}
    index, structured, counts = artifact_evidence_index({'CONTROL-EXIT': fragment})
    verdict = judge_scoring_row({'label': '项目退出移交方案', 'section_ids': ['CONTROL-EXIT'],
                                'entity_contract': contract, 'planned_response_cu_bound': ['CONTROL-EXIT-CU']},
                               index, structured, counts)
    return {'purpose': 'DEVELOPMENT_CONTROL_ONLY_NOT_RUN_G_CONTENT',
            'provider_calls': 0, 'source_contract': contract, 'fragment': fragment,
            'verdict': verdict, 'authored_development_control': True}
