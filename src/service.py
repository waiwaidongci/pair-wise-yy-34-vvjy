from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_date, require_number,
                     require_text)
from .repository import Repository
from .rules import (ACTIVITY_LEVELS, AUDIT_ROLES, CLEARANCE_ROLES,
                    CONCLUSION_ROLES, CREATE_ROLES, ENTITY, FINAL_WORKER_STATES,
                    FOLLOWUP_ROLES, INJURY_SEVERITIES, RECORD_ROLES, STATES,
                    TITLE, VIEW_ROLES, WORKER_ROLES, clearance_eligible,
                    completion_blockers, derive_worker_status,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition, verification_blockers)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        blockers += verification_blockers(
            self.repository.list_workers(item_id=item_id), target)
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def register_worker(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, WORKER_ROLES)
        actor = require_text(actor, "actor", 100)
        worker_name = require_text(payload.get("worker_name"), "worker_name", 100)
        worker_no = require_text(payload.get("worker_no"), "worker_no", 50)
        position = require_text(payload.get("position"), "position", 100)
        body_part = require_text(payload.get("body_part"), "body_part", 100)
        injury_severity = payload.get("injury_severity")
        if injury_severity not in INJURY_SEVERITIES:
            raise ValidationError("injury_severity不在允许范围内")
        first_visit_date = require_date(payload.get("first_visit_date"), "first_visit_date")
        expected_return_date = require_date(
            payload.get("expected_return_date"), "expected_return_date")
        if expected_return_date < first_visit_date:
            raise ValidationError("预计返岗日不能早于首诊日")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        worker = self.repository.create_worker(
            item_id, worker_name, worker_no, position, body_part, injury_severity,
            first_visit_date, expected_return_date, external_ref, actor)
        self.repository.append_audit("worker_register", ENTITY, item_id, actor, {
            "worker_id": worker["id"], "worker_no": worker_no, "position": position,
            "injury_severity": injury_severity,
        })
        return self._worker_view(worker)

    def update_worker(self, item_id: int, worker_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WORKER_ROLES)
        actor = require_text(actor, "actor", 100)
        worker = self.repository.get_item_worker(item_id, worker_id)
        fields: Dict[str, Any] = {}
        for key, limit in (("worker_name", 100), ("worker_no", 50),
                           ("position", 100), ("body_part", 100)):
            if key in payload:
                fields[key] = require_text(payload.get(key), key, limit)
        if "injury_severity" in payload:
            if payload.get("injury_severity") not in INJURY_SEVERITIES:
                raise ValidationError("injury_severity不在允许范围内")
            fields["injury_severity"] = payload.get("injury_severity")
        for key in ("first_visit_date", "expected_return_date"):
            if key in payload:
                fields[key] = require_date(payload.get(key), key)
        if not fields:
            raise ValidationError("没有需要修改的字段")
        first = fields.get("first_visit_date", worker["first_visit_date"])
        expected = fields.get("expected_return_date", worker["expected_return_date"])
        if expected < first:
            raise ValidationError("预计返岗日不能早于首诊日")
        self.repository.update_worker_fields(worker_id, fields)
        item = self.repository.get_item(item_id)
        updated = self._recompute_worker(worker_id, void_confirmations=True)
        self.repository.append_audit("worker_update", ENTITY, item_id, actor, {
            "worker_id": worker_id, "fields": sorted(fields),
            "item_status": item["status"], "clearance_voided": True,
            "worker_status": updated["status"],
        })
        return updated

    def add_followup(self, item_id: int, worker_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, FOLLOWUP_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item_worker(item_id, worker_id)
        followup = self._build_followup(worker_id, payload, actor)
        updated = self._recompute_worker(worker_id, void_confirmations=True)
        self.repository.append_audit("followup_add", ENTITY, item_id, actor, {
            "worker_id": worker_id, "followup_id": followup["id"],
            "activity_level": followup["activity_level"],
            "worker_status": updated["status"],
        })
        return followup

    def update_followup(self, item_id: int, worker_id: int, followup_id: int,
                        payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, FOLLOWUP_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item_worker(item_id, worker_id)
        self.repository.get_followup(worker_id, followup_id)
        fields: Dict[str, Any] = {}
        if "visit_date" in payload:
            fields["visit_date"] = require_date(payload.get("visit_date"), "visit_date")
        if "activity_level" in payload:
            if payload.get("activity_level") not in ACTIVITY_LEVELS:
                raise ValidationError("activity_level不在允许范围内")
            fields["activity_level"] = payload.get("activity_level")
        if "restrictions" in payload:
            fields["restrictions"] = require_text(
                payload.get("restrictions"), "restrictions")
        if "doctor_opinion" in payload:
            fields["doctor_opinion"] = require_text(
                payload.get("doctor_opinion"), "doctor_opinion")
        if not fields:
            raise ValidationError("没有需要修改的字段")
        followup = self.repository.update_followup(worker_id, followup_id, fields)
        updated = self._recompute_worker(worker_id, void_confirmations=True)
        self.repository.append_audit("followup_update", ENTITY, item_id, actor, {
            "worker_id": worker_id, "followup_id": followup_id,
            "fields": sorted(fields), "clearance_voided": True,
            "worker_status": updated["status"],
        })
        return followup

    def confirm_clearance(self, item_id: int, worker_id: int, actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, CLEARANCE_ROLES)
        actor = require_text(actor, "actor", 100)
        worker = self.repository.get_item_worker(item_id, worker_id)
        if worker["status"] in FINAL_WORKER_STATES:
            raise ConflictError("伤者已有最终结论")
        if worker["status"] == "cleared":
            raise ConflictError("伤者已获返岗许可")
        if worker["status"] != "pending_clearance":
            raise ConflictError("伤情严重、复诊未到或活动能力不达标，只能保持待返岗")
        confirmations = self.repository.list_confirmations(worker_id)
        if any(c["actor"] == actor for c in confirmations):
            raise ConflictError("同一伤者的两份确认不能是同一人")
        self.repository.add_confirmation(worker_id, role, actor)
        updated = self._recompute_worker(worker_id, void_confirmations=False)
        self.repository.append_audit("clearance_confirm", ENTITY, item_id, actor, {
            "worker_id": worker_id, "confirmer_role": role,
            "worker_status": updated["status"],
        })
        return updated

    def conclude_worker(self, item_id: int, worker_id: int, conclusion: str,
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CONCLUSION_ROLES)
        actor = require_text(actor, "actor", 100)
        if conclusion not in FINAL_WORKER_STATES:
            raise ValidationError("conclusion必须是returned或restricted")
        worker = self.repository.get_item_worker(item_id, worker_id)
        if worker["status"] in FINAL_WORKER_STATES:
            raise ConflictError("伤者已有最终结论")
        if conclusion == "returned" and worker["status"] != "cleared":
            raise ConflictError("未获返岗许可，不能登记最终返岗")
        updated = self.repository.set_worker_status(worker_id, conclusion)
        self.repository.append_audit("worker_conclusion", ENTITY, item_id, actor, {
            "worker_id": worker_id, "conclusion": conclusion,
        })
        return self._worker_view(updated)

    def list_workers(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return [self._worker_view(w) for w in self.repository.list_workers(item_id=item_id)]

    def ledger(self, role: str, position: Optional[str] = None,
               item_status: Optional[str] = None) -> Dict[str, Any]:
        self._view(role)
        if item_status and item_status not in STATES:
            raise ValidationError("未知事故状态")
        workers = self.repository.list_workers(position=position, item_status=item_status)
        views = [self._worker_view(w) for w in workers]
        groups: Dict[str, list] = {state: [] for state in
                                   ("pending_followup", "pending_clearance", "cleared",
                                    "returned", "restricted")}
        for view in views:
            groups[view["status"]].append(view)
        return {"workers": views, "groups": groups}

    def _build_followup(self, worker_id: int, payload: Dict[str, Any],
                        actor: str) -> Dict[str, Any]:
        visit_date = require_date(payload.get("visit_date"), "visit_date")
        activity_level = payload.get("activity_level")
        if activity_level not in ACTIVITY_LEVELS:
            raise ValidationError("activity_level不在允许范围内")
        restrictions = require_text(payload.get("restrictions"), "restrictions")
        doctor_opinion = require_text(payload.get("doctor_opinion"), "doctor_opinion")
        return self.repository.add_followup(
            worker_id, visit_date, activity_level, restrictions, doctor_opinion, actor)

    def _recompute_worker(self, worker_id: int, void_confirmations: bool) -> Dict[str, Any]:
        if void_confirmations:
            self.repository.delete_confirmations(worker_id)
        worker = self.repository.get_worker(worker_id)
        followups = self.repository.list_followups(worker_id)
        confirmations = self.repository.list_confirmations(worker_id)
        status = derive_worker_status(
            worker["injury_severity"], followups, confirmations)
        if status != worker["status"]:
            worker = self.repository.set_worker_status(worker_id, status)
        return self._worker_view(worker)

    def _worker_view(self, worker: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(worker)
        result["followups"] = self.repository.list_followups(worker["id"])
        result["confirmations"] = self.repository.list_confirmations(worker["id"])
        result["clearance_eligible"] = clearance_eligible(
            worker["injury_severity"], result["followups"])
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
