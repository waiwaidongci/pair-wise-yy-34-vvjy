import tempfile, unittest
from datetime import date, timedelta
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.rules import (GROUP_FOLLOWUP, GROUP_PERMIT, GROUP_RETURN, STATES,
                       TRANSITION_ROLES)


def today(offset=0):
    return (date.today() + timedelta(days=offset)).isoformat()


class WorkerLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "冲压伤害", "description": "手部压伤", "severity": "serious",
            "quantity": 2, "threshold": 1, "external_ref": "INJ-1",
        }, "reporter1", "reporter")
        self.iid = self.item["id"]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def register(self, serious=False, badge="E001"):
        return self.service.register_worker(self.iid, {
            "name": "张三", "badge": badge, "position": "冲压工",
            "injury_part": "右手", "serious": serious,
            "first_visit_date": today(-3), "expected_return_date": today(14),
        }, "safety1", "safety_manager")

    def followup(self, worker_id, mobility="full", next_visit=None,
                 visit=None, actor="safety1", opinion="恢复良好"):
        return self.service.add_follow_up(worker_id, {
            "visit_date": visit or today(-1), "mobility": mobility,
            "restrictions": "无" if mobility == "full" else "避免负重",
            "doctor_opinion": opinion, "next_visit_date": next_visit,
        }, actor, "safety_manager")

    def confirm_both(self, worker_id):
        self.service.confirm_return(worker_id, {"slot": "safety"},
                                    "safety1", "safety_manager")
        self.service.confirm_return(worker_id, {"slot": "foreman"},
                                    "boss1", "foreman")

    def advance_to_verification(self):
        current = self.service.get_item(self.iid, "viewer")
        for target in ("investigating", "corrective_action"):
            current = self.service.transition(
                current["id"], target, current["version"], "inv1",
                TRANSITION_ROLES[target][0])
        return current

    def test_registered_worker_starts_in_followup_group(self):
        worker = self.register()
        self.assertEqual(worker["group"], GROUP_FOLLOWUP)
        self.assertIn("no_followup", worker["medical_blockers"])
        board = self.service.worker_board("viewer", position="冲压")
        groups = {g["key"]: g["workers"] for g in board["groups"]}
        self.assertEqual(len(groups[GROUP_FOLLOWUP]), 1)

    def test_full_followup_moves_to_permit_pending(self):
        worker = self.register()
        self.followup(worker["id"])
        refreshed = self.service.get_worker(worker["id"], "viewer")
        self.assertEqual(refreshed["group"], GROUP_PERMIT)
        self.assertEqual(refreshed["medical_blockers"], [])

    def test_mobility_not_full_keeps_worker_waiting(self):
        worker = self.register()
        self.followup(worker["id"], mobility="partial")
        refreshed = self.service.get_worker(worker["id"], "viewer")
        self.assertEqual(refreshed["group"], GROUP_FOLLOWUP)
        self.assertIn("mobility_not_full", refreshed["medical_blockers"])
        with self.assertRaises(ConflictError):
            self.service.confirm_return(worker["id"], {"slot": "safety"},
                                        "safety1", "safety_manager")

    def test_overdue_followup_keeps_worker_waiting(self):
        worker = self.register()
        self.followup(worker["id"], next_visit=today())
        refreshed = self.service.get_worker(worker["id"], "viewer")
        self.assertIn("followup_overdue", refreshed["medical_blockers"])
        self.assertEqual(refreshed["group"], GROUP_FOLLOWUP)

    def test_serious_injury_blocks_return_permit_and_return_conclusion(self):
        worker = self.register(serious=True)
        self.followup(worker["id"])
        with self.assertRaises(ConflictError):
            self.service.confirm_return(worker["id"], {"slot": "safety"},
                                        "safety1", "safety_manager")
        # 伤情严重时只能给出长期限制结论
        with self.assertRaises(ConflictError):
            self.service.conclude_worker(worker["id"], {
                "conclusion": "returned", "conclusion_remark": "返岗"},
                "safety1", "safety_manager")
        concluded = self.service.conclude_worker(worker["id"], {
            "conclusion": "long_term_restriction",
            "conclusion_remark": "不得再从事冲压岗位",
        }, "safety1", "safety_manager")
        self.assertEqual(concluded["group"], GROUP_RETURN)

    def test_two_different_confirmations_required(self):
        worker = self.register()
        self.followup(worker["id"])
        self.service.confirm_return(worker["id"], {"slot": "safety"},
                                    "safety1", "safety_manager")
        # 同一人不能占两个槽位
        with self.assertRaises(ConflictError):
            self.service.confirm_return(worker["id"], {"slot": "foreman"},
                                        "safety1", "foreman")
        # 角色与槽位必须匹配
        with self.assertRaises(PermissionDenied):
            self.service.confirm_return(worker["id"], {"slot": "safety"},
                                        "boss1", "foreman")
        self.service.confirm_return(worker["id"], {"slot": "foreman"},
                                    "boss1", "foreman")
        refreshed = self.service.get_worker(worker["id"], "viewer")
        self.assertTrue(refreshed["permit_complete"])
        self.assertEqual(refreshed["group"], GROUP_RETURN)

    def test_conclusion_requires_complete_permit(self):
        worker = self.register()
        self.followup(worker["id"])
        with self.assertRaises(ConflictError):
            self.service.conclude_worker(worker["id"], {
                "conclusion": "returned", "conclusion_remark": "ok"},
                "safety1", "safety_manager")
        self.confirm_both(worker["id"])
        concluded = self.service.conclude_worker(worker["id"], {
            "conclusion": "returned", "conclusion_remark": "已返岗"},
            "safety1", "safety_manager")
        self.assertEqual(concluded["conclusion"], "returned")

    def test_verification_blocked_until_all_concluded(self):
        worker = self.register()
        self.followup(worker["id"])
        self.confirm_both(worker["id"])
        current = self.advance_to_verification()
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "verification",
                                    current["version"], "sm",
                                    TRANSITION_ROLES["verification"][0])
        self.service.conclude_worker(worker["id"], {
            "conclusion": "returned", "conclusion_remark": "已返岗"},
            "safety1", "safety_manager")
        current = self.service.get_item(self.iid, "viewer")
        current = self.service.transition(
            current["id"], "verification", current["version"], "sm",
            TRANSITION_ROLES["verification"][0])
        closed = self.service.transition(
            current["id"], "closed", current["version"], "sm",
            TRANSITION_ROLES["closed"][0])
        self.assertEqual(closed["status"], STATES[-1])

    def test_injury_edit_after_close_voids_permit_and_reopens(self):
        worker = self.register()
        self.followup(worker["id"])
        self.confirm_both(worker["id"])
        self.service.conclude_worker(worker["id"], {
            "conclusion": "returned", "conclusion_remark": "已返岗"},
            "safety1", "safety_manager")
        current = self.advance_to_verification()
        current = self.service.transition(
            current["id"], "verification", current["version"], "sm",
            TRANSITION_ROLES["verification"][0])
        self.service.transition(current["id"], "closed",
                                current["version"], "sm",
                                TRANSITION_ROLES["closed"][0])
        # 改姓名等登记资料不影响许可
        refreshed = self.service.update_worker(worker["id"], {"name": "张三丰"},
                                               "safety1", "safety_manager")
        self.assertFalse(refreshed["permit_voided"])
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"],
                         "closed")
        # 改伤情：许可作废、结论清空、事故回到待处理
        updated = self.service.update_worker(worker["id"], {"serious": True},
                                             "safety1", "safety_manager")
        self.assertTrue(updated["permit_voided"])
        self.assertIsNone(updated["conclusion"])
        self.assertEqual(updated["group"], GROUP_FOLLOWUP)
        item = self.service.get_item(self.iid, "viewer")
        self.assertEqual(item["status"], "corrective_action")
        self.assertEqual(item["version"], 6)

    def test_new_followup_after_close_reopens(self):
        worker = self.register()
        self.followup(worker["id"])
        self.confirm_both(worker["id"])
        self.service.conclude_worker(worker["id"], {
            "conclusion": "returned", "conclusion_remark": "已返岗"},
            "safety1", "safety_manager")
        current = self.advance_to_verification()
        current = self.service.transition(
            current["id"], "verification", current["version"], "sm",
            TRANSITION_ROLES["verification"][0])
        self.service.transition(current["id"], "closed",
                                current["version"], "sm",
                                TRANSITION_ROLES["closed"][0])
        self.followup(worker["id"], mobility="partial", visit=today())
        item = self.service.get_item(self.iid, "viewer")
        self.assertEqual(item["status"], "corrective_action")
        refreshed = self.service.get_worker(worker["id"], "viewer")
        self.assertTrue(refreshed["permit_voided"])
        self.assertFalse(refreshed["permit_complete"])

    def test_followup_voids_permit_before_close(self):
        worker = self.register()
        self.followup(worker["id"], mobility="partial")
        # 活动能力不达标时无法确认许可；这里直接构造已确认后被复诊推翻的场景
        self.followup(worker["id"], mobility="full", visit=today(-1))
        self.confirm_both(worker["id"])
        self.followup(worker["id"], mobility="restricted", visit=today())
        refreshed = self.service.get_worker(worker["id"], "viewer")
        self.assertTrue(refreshed["permit_voided"])
        self.assertEqual(refreshed["group"], GROUP_FOLLOWUP)

    def test_board_filters_by_position_and_item_status(self):
        self.register(badge="E001")
        other = self.service.create_item({
            "title": "摔伤", "description": "x", "severity": "minor",
            "quantity": 1, "threshold": 1, "external_ref": "INJ-2",
        }, "reporter1", "reporter")
        self.service.register_worker(other["id"], {
            "name": "李四", "badge": "E100", "position": "电工",
            "injury_part": "左腿", "serious": False,
            "first_visit_date": today(-2), "expected_return_date": today(7),
        }, "safety1", "safety_manager")
        board = self.service.worker_board("viewer", position="电工")
        total = sum(len(g["workers"]) for g in board["groups"])
        self.assertEqual(total, 1)
        board = self.service.worker_board("viewer", item_status="reported")
        total = sum(len(g["workers"]) for g in board["groups"])
        self.assertEqual(total, 2)
        board = self.service.worker_board("viewer", item_status="closed")
        total = sum(len(g["workers"]) for g in board["groups"])
        self.assertEqual(total, 0)

    def test_duplicate_badge_rejected(self):
        self.register(badge="E001")
        with self.assertRaises(ConflictError):
            self.register(badge="E001")

    def test_reporter_cannot_register_or_confirm(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_worker(self.iid, {
                "name": "王五", "badge": "E002", "position": "钳工",
                "injury_part": "肩", "serious": False,
                "first_visit_date": today(-1), "expected_return_date": today(5),
            }, "rep", "reporter")
        # reporter 可查看但不能登记伤者
        self.assertTrue(self.service.worker_board("reporter")["groups"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_return(1, {"slot": "foreman"},
                                        "rep", "reporter")

    def test_invalid_dates_and_choices(self):
        with self.assertRaises(ValidationError):
            self.service.register_worker(self.iid, {
                "name": "赵六", "badge": "E003", "position": "钳工",
                "injury_part": "肩", "serious": False,
                "first_visit_date": "2026/01/01",
                "expected_return_date": today(5)},
                "safety1", "safety_manager")
        worker = self.register(badge="E003")
        with self.assertRaises(ValidationError):
            self.followup(worker["id"], mobility="marathon")

    def test_long_term_restriction_requires_followup(self):
        worker = self.register(serious=True)
        with self.assertRaises(ConflictError):
            self.service.conclude_worker(worker["id"], {
                "conclusion": "long_term_restriction",
                "conclusion_remark": "待复诊再说"},
                "safety1", "safety_manager")

    def test_foreman_can_view_board(self):
        self.register()
        board = self.service.worker_board("foreman")
        self.assertEqual([g["key"] for g in board["groups"]],
                         [GROUP_FOLLOWUP, GROUP_PERMIT, GROUP_RETURN])


if __name__ == "__main__":
    unittest.main()
