from __future__ import annotations
from .domain import (CONFIRM_SLOTS, MOBILITY_LEVELS, WORKER_CONCLUSIONS,
                     ConflictError, ValidationError)
TITLE='工伤事故调查与纠正措施'; ENTITY='事故'; ID_PREFIX='OI'
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; TRANSITIONS={'reported': ['investigating'], 'investigating': ['corrective_action'], 'corrective_action': ['verification'], 'verification': ['closed'], 'closed': []}; TRANSITION_ROLES={'investigating': ['investigator'], 'corrective_action': ['investigator'], 'verification': ['safety_manager'], 'closed': ['safety_manager']}
CREATE_ROLES=set(['reporter', 'investigator']); RECORD_ROLES=set(['investigator', 'safety_manager']); AUDIT_ROLES=set(['safety_manager', 'viewer']); VIEW_ROLES=set(['reporter', 'investigator', 'safety_manager', 'foreman', 'viewer'])
WORKER_MANAGE_ROLES=set(['investigator', 'safety_manager'])
GROUP_FOLLOWUP='followup_due'; GROUP_PERMIT='permit_pending'; GROUP_RETURN='return_ready'
BOARD_GROUPS=[(GROUP_FOLLOWUP,'待复诊'),(GROUP_PERMIT,'待许可'),(GROUP_RETURN,'可返岗')]
# 安全员和班组长分别持有一个许可确认槽位
CONFIRM_SLOT_ROLES={'safety': 'safety_manager', 'foreman': 'foreman'}
REOPEN_STATE='corrective_action'
SEVERITY_WEIGHT={'minor': 1.0, 'moderate': 3.0, 'serious': 6.0, 'fatal': 9.0}; DEADLINE_HOURS={'minor': 72, 'moderate': 24, 'serious': 8, 'fatal': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def latest_followup(followups):
    return sorted(followups,key=lambda f:f["visit_date"])[-1] if followups else None
def medical_blockers(worker,followups,today):
    """伤者只能保持待返岗的医学原因：伤情严重、复诊未到、活动能力不达标。"""
    blockers=[]
    if worker.get("serious"): blockers.append("serious_injury")
    followup=latest_followup(followups)
    if followup is None:
        blockers.append("no_followup")
    else:
        if followup["mobility"]!=MOBILITY_LEVELS[-1]: blockers.append("mobility_not_full")
        due=followup.get("next_visit_date")
        if due and due<=today and followup["visit_date"]<=today: blockers.append("followup_overdue")
    return blockers
def permit_actors(worker):
    """当前有效许可的两份确认（槽位->确认人）。"""
    actors={}
    if not worker.get("permit_voided"):
        if worker.get("safety_actor"): actors["safety"]=worker["safety_actor"]
        if worker.get("foreman_actor"): actors["foreman"]=worker["foreman_actor"]
    return actors
def permit_complete(worker):
    actors=permit_actors(worker)
    return "safety" in actors and "foreman" in actors
def worker_group(worker,followups,today):
    if worker.get("conclusion"): return GROUP_RETURN
    if medical_blockers(worker,followups,today): return GROUP_FOLLOWUP
    if not permit_complete(worker): return GROUP_PERMIT
    return GROUP_RETURN
def conclusion_blockers(worker,followups,today):
    """事故进入验证前，每位伤者都必须有最终返岗或长期限制结论。"""
    if worker.get("conclusion"): return []
    return [f"伤者{worker.get('name') or worker['id']}尚无最终返岗或长期限制结论"]
