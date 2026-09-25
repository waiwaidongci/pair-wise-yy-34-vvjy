import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class InjuredWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "冲压机伤害事故", "description": "操作工左手受伤",
             "severity": "serious", "quantity": 3, "threshold": 1,
             "external_ref": "IW-1"}, "creator", "reporter")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _worker(self, **kw):
        payload = {"worker_name": "张三", "worker_no": "E100", "position": "冲压工",
                   "body_part": "左手", "injury_severity": "minor",
                   "first_visit_date": "2026-09-01", "expected_return_date": "2026-09-20"}
        payload.update(kw)
        return self.service.register_worker(self.item["id"], payload, "安全员甲", "safety_manager")

    def _followup(self, worker_id, level="full", date="2026-09-10"):
        return self.service.add_followup(
            self.item["id"], worker_id,
            {"visit_date": date, "activity_level": level,
             "restrictions": "无", "doctor_opinion": "恢复良好"},
            "安全员甲", "safety_manager")

    def _clear(self, worker_id):
        self.service.confirm_clearance(self.item["id"], worker_id, "安全员甲", "safety_manager")
        return self.service.confirm_clearance(self.item["id"], worker_id, "班组长乙", "team_leader")

    def _advance(self, targets):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in targets:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_followup_and_dual_confirmation(self):
        worker = self._worker()
        self.assertEqual(worker["status"], "pending_followup")
        self._followup(worker["id"], "partial")
        view = self.service.list_workers(self.item["id"], "viewer")[0]
        self.assertEqual(view["status"], "pending_clearance")
        self.assertTrue(view["clearance_eligible"])
        self.service.confirm_clearance(self.item["id"], worker["id"], "安全员甲", "safety_manager")
        view = self.service.list_workers(self.item["id"], "viewer")[0]
        self.assertEqual(view["status"], "pending_clearance")
        self.assertEqual(len(view["confirmations"]), 1)
        done = self.service.confirm_clearance(self.item["id"], worker["id"], "班组长乙", "team_leader")
        self.assertEqual(done["status"], "cleared")
        self.assertEqual(len(done["confirmations"]), 2)

    def test_confirmation_actor_and_role_guards(self):
        worker = self._worker()
        self._followup(worker["id"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_clearance(self.item["id"], worker["id"], "访客", "viewer")
        self.service.confirm_clearance(self.item["id"], worker["id"], "安全员甲", "safety_manager")
        with self.assertRaises(ConflictError):
            self.service.confirm_clearance(self.item["id"], worker["id"], "安全员甲", "safety_manager")
        with self.assertRaises(ConflictError):
            self.service.confirm_clearance(self.item["id"], worker["id"], "安全员甲", "team_leader")
        done = self.service.confirm_clearance(self.item["id"], worker["id"], "班组长乙", "team_leader")
        self.assertEqual(done["status"], "cleared")
        with self.assertRaises(ConflictError):
            self.service.confirm_clearance(self.item["id"], worker["id"], "班组长丙", "team_leader")

    def test_blocked_conditions_keep_pending(self):
        severe = self._worker(worker_no="E101", injury_severity="severe")
        with self.assertRaises(ConflictError):
            self.service.confirm_clearance(self.item["id"], severe["id"], "安全员甲", "safety_manager")
        self._followup(severe["id"], "full")
        with self.assertRaises(ConflictError):
            self.service.confirm_clearance(self.item["id"], severe["id"], "安全员甲", "safety_manager")
        healed = self.service.update_worker(
            self.item["id"], severe["id"], {"injury_severity": "moderate"}, "安全员甲", "safety_manager")
        self.assertEqual(healed["status"], "pending_clearance")

        weak = self._worker(worker_no="E102")
        self._followup(weak["id"], "limited")
        view = [w for w in self.service.list_workers(self.item["id"], "viewer")
                if w["id"] == weak["id"]][0]
        self.assertEqual(view["status"], "pending_followup")
        with self.assertRaises(ConflictError):
            self.service.confirm_clearance(self.item["id"], weak["id"], "安全员甲", "safety_manager")

    def test_verification_requires_final_conclusions(self):
        worker = self._worker()
        self._advance(["investigating", "corrective_action"])
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "verification", current["version"],
                                    "reviewer", "safety_manager")
        self._followup(worker["id"])
        self._clear(worker["id"])
        unfit = self._worker(worker_no="E103")
        with self.assertRaises(ConflictError):
            self.service.conclude_worker(self.item["id"], unfit["id"],
                                         "returned", "安全员甲", "safety_manager")
        done = self.service.conclude_worker(self.item["id"], worker["id"], "returned",
                                            "安全员甲", "safety_manager")
        self.assertEqual(done["status"], "returned")
        self.service.conclude_worker(self.item["id"], unfit["id"], "restricted",
                                     "安全员甲", "safety_manager")
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], "verification", current["version"],
                                          "reviewer", "safety_manager")
        self.assertEqual(current["status"], "verification")

    def test_modification_after_closure_voids_clearance(self):
        worker = self._worker()
        self._followup(worker["id"])
        self._clear(worker["id"])
        self.service.conclude_worker(self.item["id"], worker["id"], "returned",
                                     "安全员甲", "safety_manager")
        current = self._advance(["investigating", "corrective_action", "verification", "closed"])
        self.assertEqual(current["status"], "closed")
        updated = self.service.update_worker(
            self.item["id"], worker["id"], {"injury_severity": "moderate"},
            "安全员甲", "safety_manager")
        self.assertEqual(updated["status"], "pending_clearance")
        self.assertEqual(updated["confirmations"], [])
        self._clear(worker["id"])
        self.service.update_followup(
            self.item["id"], worker["id"],
            self.service.list_workers(self.item["id"], "viewer")[0]["followups"][0]["id"],
            {"activity_level": "limited"}, "安全员甲", "safety_manager")
        view = self.service.list_workers(self.item["id"], "viewer")[0]
        self.assertEqual(view["status"], "pending_followup")
        self.assertEqual(view["confirmations"], [])

    def test_ledger_filters_and_groups(self):
        other = self.service.create_item(
            {"title": "叉车碰撞", "description": "腿部擦伤", "severity": "minor",
             "quantity": 1, "threshold": 5, "external_ref": "IW-2"}, "creator", "reporter")
        w1 = self._worker()
        self._worker(worker_no="E105", position="装配工")
        self.service.register_worker(other["id"], {
            "worker_name": "李四", "worker_no": "E200", "position": "冲压工",
            "body_part": "右腿", "injury_severity": "moderate",
            "first_visit_date": "2026-09-02", "expected_return_date": "2026-09-15"},
            "安全员甲", "safety_manager")
        self._followup(w1["id"])
        ledger = self.service.ledger("viewer")
        self.assertEqual(len(ledger["workers"]), 3)
        self.assertEqual(len(ledger["groups"]["pending_clearance"]), 1)
        self.assertEqual(len(ledger["groups"]["pending_followup"]), 2)
        by_position = self.service.ledger("viewer", position="冲压工")
        self.assertEqual(len(by_position["workers"]), 2)
        self.service.transition(other["id"], "investigating", other["version"],
                                "reviewer", "investigator")
        by_status = self.service.ledger("viewer", item_status="investigating")
        self.assertEqual(len(by_status["workers"]), 1)
        self.assertEqual(by_status["workers"][0]["worker_name"], "李四")
        with self.assertRaises(ValidationError):
            self.service.ledger("viewer", item_status="not-a-state")

    def test_validation_and_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_worker(self.item["id"], {}, "x", "team_leader")
        with self.assertRaises(ValidationError):
            self._worker(first_visit_date="2026-09-20", expected_return_date="2026-09-01")
        with self.assertRaises(ValidationError):
            self._worker(first_visit_date="not-a-date")
        worker = self._worker()
        with self.assertRaises(ValidationError):
            self.service.add_followup(self.item["id"], worker["id"],
                                      {"visit_date": "2026-09-10", "activity_level": "great",
                                       "restrictions": "无", "doctor_opinion": "好"},
                                      "安全员甲", "safety_manager")
        with self.assertRaises(ValidationError):
            self.service.conclude_worker(self.item["id"], worker["id"], "maybe",
                                         "安全员甲", "safety_manager")
        dup = self._worker(worker_no="E106", external_ref="W-EXT-1")
        self.assertEqual(dup["external_ref"], "W-EXT-1")
        with self.assertRaises(ConflictError):
            self._worker(worker_no="E107", external_ref="W-EXT-1")


if __name__ == "__main__":
    unittest.main()
