from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['minor', 'moderate', 'serious', 'fatal']; STATES=['reported', 'investigating', 'corrective_action', 'verification', 'closed']; ROLES=['reporter', 'investigator', 'safety_manager', 'team_leader', 'viewer']
INJURY_SEVERITIES=['minor', 'moderate', 'severe']; ACTIVITY_LEVELS=['limited', 'partial', 'full']; WORKER_STATES=['pending_followup', 'pending_clearance', 'cleared', 'returned', 'restricted']; FINAL_WORKER_STATES=['returned', 'restricted']; CLEARANCE_ROLES=['safety_manager', 'team_leader']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
@dataclass(frozen=True)
class InjuredWorker:
    id:int; item_id:int; worker_name:str; worker_no:str; position:str; body_part:str; injury_severity:str; first_visit_date:str; expected_return_date:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Followup:
    id:int; worker_id:int; visit_date:str; activity_level:str; restrictions:str; doctor_opinion:str; created_by:str; created_at:str
@dataclass(frozen=True)
class ClearanceConfirmation:
    id:int; worker_id:int; confirmer_role:str; actor:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def require_date(value,field):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    try: date.fromisoformat(value)
    except ValueError: raise ValidationError(f"{field}必须是YYYY-MM-DD格式的日期")
    return value
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
