from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from .domain import (CONFIRM_SLOTS, MOBILITY_LEVELS, WORKER_CONCLUSIONS,
                     ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, optional_date,
                     require_bool, require_choice, require_date, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BOARD_GROUPS, CONFIRM_SLOT_ROLES, CREATE_ROLES,
                    ENTITY, GROUP_FOLLOWUP, GROUP_PERMIT, GROUP_RETURN,
                    MOBILITY_LEVELS, RECORD_ROLES, REOPEN_STATE, TITLE,
                    VIEW_ROLES, WORKER_CONCLUSIONS, WORKER_MANAGE_ROLES,
                    completion_blockers, conclusion_blockers, escalation_required,
                    latest_followup, medical_blockers, permit_complete,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition, worker_group)


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
        if blockers:
            raise ConflictError("；".join(blockers))
        if target == "verification":
            blockers = self._worker_conclusion_blockers(item_id)
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

    # ---- 伤者台账 ----------------------------------------------------------
    def register_worker(self, item_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WORKER_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        worker = self._build_worker_fields(payload)
        created = self.repository.create_worker(item_id, actor=actor, **worker)
        self.repository.append_audit("worker_register", "伤者", created["id"], actor, {
            "item_id": item_id, "name": worker["name"], "badge": worker["badge"],
            "serious": bool(worker["serious"]),
        })
        return self._enrich_worker(created)

    def update_worker(self, worker_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WORKER_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        existing = self.repository.get_worker(worker_id)
        merged = {
            "name": payload.get("name", existing["name"]),
            "badge": payload.get("badge", existing["badge"]),
            "position": payload.get("position", existing["position"]),
            "injury_part": payload.get("injury_part", existing["injury_part"]),
            "serious": payload.get("serious", bool(existing["serious"])),
            "first_visit_date": payload.get("first_visit_date",
                                            existing["first_visit_date"]),
            "expected_return_date": payload.get("expected_return_date",
                                                existing["expected_return_date"]),
        }
        fields = self._build_worker_fields(merged)
        updated, reopened, medical_changed = self.repository.update_worker_fields(
            worker_id, fields)
        if medical_changed:
            self.repository.append_audit("worker_permit_void", "伤者", worker_id, actor, {
                "item_id": updated["item_id"], "reason": "伤情资料变更",
                "reopened": reopened,
            })
            if reopened:
                self.repository.append_audit(
                    "reopen", ENTITY, updated["item_id"], actor,
                    {"from": "closed", "to": REOPEN_STATE,
                     "reason": "已关档事故修改伤者伤情资料，原许可作废"})
        else:
            self.repository.append_audit("worker_update", "伤者", worker_id, actor, {
                "item_id": updated["item_id"]})
        return self._enrich_worker(updated)

    def add_follow_up(self, worker_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WORKER_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        worker = self.repository.get_worker(worker_id)
        visit_date = require_date(payload.get("visit_date"), "visit_date")
        mobility = require_choice(payload.get("mobility"), "mobility", MOBILITY_LEVELS)
        restrictions = require_text(payload.get("restrictions"), "restrictions", 1000)
        doctor_opinion = require_text(payload.get("doctor_opinion"),
                                      "doctor_opinion", 2000)
        next_visit_date = optional_date(payload.get("next_visit_date"),
                                        "next_visit_date")
        followup, reopened = self.repository.add_follow_up(
            worker_id, visit_date, mobility, restrictions, doctor_opinion,
            next_visit_date, actor)
        self.repository.append_audit("followup_add", "伤者复诊", followup["id"], actor, {
            "worker_id": worker_id, "item_id": worker["item_id"],
            "mobility": mobility, "reopened": reopened})
        if reopened:
            self.repository.append_audit(
                "reopen", ENTITY, worker["item_id"], actor,
                {"from": "closed", "to": REOPEN_STATE,
                 "reason": "已关档事故补充复诊资料，原许可作废"})
        return followup

    def confirm_return(self, worker_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        slot = payload.get("slot")
        if slot not in CONFIRM_SLOT_ROLES:
            raise ValidationError("slot必须是safety或foreman")
        if role != CONFIRM_SLOT_ROLES[slot]:
            raise PermissionDenied("当前角色无权在该确认槽位确认")
        worker = self.repository.get_worker(worker_id)
        followups = self.repository.list_follow_ups(worker_id)
        blockers = medical_blockers(worker, followups, self._today())
        if blockers:
            raise ConflictError("伤者仍需保持待返岗：" + "、".join(
                self._BLOCKER_LABELS[b] for b in blockers))
        updated = self.repository.confirm_permit(worker_id, slot, actor)
        self.repository.append_audit("permit_confirm", "伤者", worker_id, actor, {
            "item_id": worker["item_id"], "slot": slot,
            "permit_complete": permit_complete(updated)})
        return self._enrich_worker(updated)

    def conclude_worker(self, worker_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, WORKER_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = require_choice(payload.get("conclusion"), "conclusion",
                                    WORKER_CONCLUSIONS)
        remark = require_text(payload.get("conclusion_remark"),
                              "conclusion_remark", 2000)
        worker = self.repository.get_worker(worker_id)
        followups = self.repository.list_follow_ups(worker_id)
        today = self._today()
        if conclusion == "returned":
            blockers = medical_blockers(worker, followups, today)
            if blockers:
                raise ConflictError("伤者仍需保持待返岗，不能给出最终返岗结论："
                                    + "、".join(self._BLOCKER_LABELS[b]
                                                for b in blockers))
            if not permit_complete(worker):
                raise ConflictError("返岗结论需要安全员和班组长的双份确认")
        else:
            if not followups:
                raise ConflictError("长期限制结论需要至少一次复诊记录及医生意见")
        updated = self.repository.set_conclusion(worker_id, conclusion, remark)
        self.repository.append_audit("worker_conclude", "伤者", worker_id, actor, {
            "item_id": worker["item_id"], "conclusion": conclusion})
        return self._enrich_worker(updated)

    def get_worker(self, worker_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        worker = self.repository.get_worker(worker_id)
        return self._enrich_worker(worker)

    def list_workers(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return [self._enrich_worker(w)
                for w in self.repository.list_workers_for_item(item_id)]

    def worker_board(self, role: str, position: Optional[str] = None,
                     item_status: Optional[str] = None) -> Dict[str, Any]:
        """按待复诊、待许可、可返岗分组的伤者台账，可按岗位和事故状态筛选。"""
        self._view(role)
        if item_status and item_status not in [
                "reported", "investigating", "corrective_action",
                "verification", "closed"]:
            raise ValidationError("status不在允许范围内")
        today = self._today()
        items = {i["id"]: i for i in self.repository.list_items(item_status)}
        groups = {key: [] for key, _ in BOARD_GROUPS}
        for worker in self.repository.list_all_workers(position, item_status):
            if worker["item_id"] not in items:
                continue
            enriched = self._enrich_worker(worker)
            groups[enriched["group"]].append(enriched)
        return {"today": today,
                "groups": [{"key": key, "label": label,
                            "workers": groups[key]} for key, label in BOARD_GROUPS]}

    def _worker_conclusion_blockers(self, item_id: int) -> list:
        blockers = []
        for worker in self.repository.list_workers_for_item(item_id):
            blockers.extend(conclusion_blockers(worker, [], self._today()))
        return blockers

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    _BLOCKER_LABELS = {
        "serious_injury": "伤情严重",
        "no_followup": "尚无复诊记录",
        "followup_overdue": "复诊到期未完成",
        "mobility_not_full": "活动能力不达标",
    }

    @staticmethod
    def _build_worker_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": require_text(payload.get("name"), "name", 100),
            "badge": require_text(payload.get("badge"), "badge", 60),
            "position": require_text(payload.get("position"), "position", 100),
            "injury_part": require_text(payload.get("injury_part"),
                                        "injury_part", 200),
            "serious": require_bool(payload.get("serious", False), "serious"),
            "first_visit_date": require_date(payload.get("first_visit_date"),
                                             "first_visit_date"),
            "expected_return_date": require_date(
                payload.get("expected_return_date"), "expected_return_date"),
        }

    def _enrich_worker(self, worker: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(worker)
        result["serious"] = bool(worker["serious"])
        result["permit_voided"] = bool(worker["permit_voided"])
        followups = self.repository.list_follow_ups(worker["id"])
        today = self._today()
        result["follow_ups"] = followups
        result["latest_follow_up"] = latest_followup(followups)
        result["medical_blockers"] = medical_blockers(worker, followups, today)
        result["permit_complete"] = permit_complete(worker)
        result["group"] = worker_group(worker, followups, today)
        item = self.repository.get_item(worker["item_id"])
        result["item_status"] = item["status"]
        result["item_title"] = item["title"]
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
