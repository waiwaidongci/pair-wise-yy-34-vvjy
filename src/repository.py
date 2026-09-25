from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    badge TEXT NOT NULL,
                    position TEXT NOT NULL,
                    injury_part TEXT NOT NULL,
                    serious INTEGER NOT NULL DEFAULT 0,
                    first_visit_date TEXT NOT NULL,
                    expected_return_date TEXT NOT NULL,
                    conclusion TEXT,
                    conclusion_remark TEXT,
                    permit_voided INTEGER NOT NULL DEFAULT 0,
                    safety_actor TEXT,
                    safety_confirmed_at TEXT,
                    foreman_actor TEXT,
                    foreman_confirmed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, badge)
                );
                CREATE TABLE IF NOT EXISTS follow_ups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
                    visit_date TEXT NOT NULL,
                    mobility TEXT NOT NULL,
                    restrictions TEXT NOT NULL,
                    doctor_opinion TEXT NOT NULL,
                    next_visit_date TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workers_item ON workers(item_id);
                CREATE INDEX IF NOT EXISTS ix_followups_worker ON follow_ups(worker_id);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_worker(self, item_id: int, name: str, badge: str, position: str,
                      injury_part: str, serious: int, first_visit_date: str,
                      expected_return_date: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO workers(item_id, name, badge, position, injury_part,
                       serious, first_visit_date, expected_return_date,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, name, badge, position, injury_part, serious,
                     first_visit_date, expected_return_date, actor, now, now),
                )
                worker_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一事故下员工编号已存在") from exc
        return self.get_worker(worker_id)

    WORKER_FIELDS = ("name", "badge", "position", "injury_part", "serious",
                     "first_visit_date", "expected_return_date")

    def update_worker_fields(self, worker_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        """更新伤者登记资料；伤情相关字段变化时许可作废，事故已关档则回到待处理。"""
        medical_fields = ("injury_part", "serious", "first_visit_date",
                          "expected_return_date")
        columns = ",".join(f"{k}=?" for k in self.WORKER_FIELDS)
        values = [changes[k] for k in self.WORKER_FIELDS] + [utc_now(), worker_id]
        with self._lock, self.conn:
            before = self.conn.execute(
                "SELECT * FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
            if before is None:
                raise NotFoundError("伤者不存在")
            medical_changed = any(before[k] != changes[k] for k in medical_fields)
            self.conn.execute(
                f"UPDATE workers SET {columns}, updated_at=? WHERE id=?", values)
            reopened = False
            if medical_changed and not before["permit_voided"]:
                self.conn.execute(
                    """UPDATE workers SET permit_voided=1, safety_actor=NULL,
                       safety_confirmed_at=NULL, foreman_actor=NULL,
                       foreman_confirmed_at=NULL, conclusion=NULL,
                       conclusion_remark=NULL WHERE id=?""",
                    (worker_id,),
                )
                item = self.conn.execute(
                    "SELECT status FROM items WHERE id=?", (before["item_id"],)
                ).fetchone()
                if item["status"] == "closed":
                    self.conn.execute(
                        """UPDATE items SET status='corrective_action',
                           version=version+1, updated_at=? WHERE id=?""",
                        (utc_now(), before["item_id"]),
                    )
                    reopened = True
        return self.get_worker(worker_id), reopened, medical_changed

    def get_worker(self, worker_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("伤者不存在")
        return dict(row)

    def list_workers_for_item(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM workers WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_all_workers(self, position: Optional[str] = None,
                         status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT w.* FROM workers w JOIN items i ON w.item_id=i.id"
        clauses = []
        params: List[Any] = []
        if position:
            clauses.append("w.position LIKE ?")
            params.append(f"%{position}%")
        if status:
            clauses.append("i.status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY w.item_id DESC, w.id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_follow_ups(self, worker_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM follow_ups WHERE worker_id=? ORDER BY visit_date, id",
                (worker_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_follow_up(self, worker_id: int, visit_date: str, mobility: str,
                      restrictions: str, doctor_opinion: str,
                      next_visit_date: Optional[str], actor: str
                      ) -> Tuple[Dict[str, Any], bool]:
        """登记复诊；新复诊资料使原许可作废，事故已关档则回到待处理。"""
        now = utc_now()
        with self._lock, self.conn:
            worker = self.conn.execute(
                "SELECT * FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
            if worker is None:
                raise NotFoundError("伤者不存在")
            cur = self.conn.execute(
                """INSERT INTO follow_ups(worker_id, visit_date, mobility, restrictions,
                   doctor_opinion, next_visit_date, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (worker_id, visit_date, mobility, restrictions, doctor_opinion,
                 next_visit_date, actor, now),
            )
            followup_id = int(cur.lastrowid)
            reopened = False
            if not worker["permit_voided"] and (worker["safety_actor"]
                                                or worker["foreman_actor"]
                                                or worker["conclusion"]):
                self.conn.execute(
                    """UPDATE workers SET permit_voided=1, safety_actor=NULL,
                       safety_confirmed_at=NULL, foreman_actor=NULL,
                       foreman_confirmed_at=NULL, conclusion=NULL,
                       conclusion_remark=NULL, updated_at=? WHERE id=?""",
                    (now, worker_id),
                )
                item = self.conn.execute(
                    "SELECT status FROM items WHERE id=?", (worker["item_id"],)
                ).fetchone()
                if item["status"] == "closed":
                    self.conn.execute(
                        """UPDATE items SET status='corrective_action',
                           version=version+1, updated_at=? WHERE id=?""",
                        (now, worker["item_id"]),
                    )
                    reopened = True
            row = self.conn.execute(
                "SELECT * FROM follow_ups WHERE id=?", (followup_id,)
            ).fetchone()
        return dict(row), reopened

    def confirm_permit(self, worker_id: int, slot: str, actor: str) -> Dict[str, Any]:
        """在对应确认槽位写入确认人；安全员与班组长不能是同一人。"""
        now = utc_now()
        with self._lock, self.conn:
            worker = self.conn.execute(
                "SELECT * FROM workers WHERE id=?", (worker_id,)
            ).fetchone()
            if worker is None:
                raise NotFoundError("伤者不存在")
            other = "foreman" if slot == "safety" else "safety"
            other_actor = worker[f"{other}_actor"]
            if other_actor and other_actor == actor:
                raise ConflictError("安全员和班组长的两份确认不能是同一人")
            self.conn.execute(
                f"UPDATE workers SET {slot}_actor=?, {slot}_confirmed_at=?, "
                "permit_voided=0, updated_at=? WHERE id=?",
                (actor, now, now, worker_id),
            )
        return self.get_worker(worker_id)

    def set_conclusion(self, worker_id: int, conclusion: str,
                       remark: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE workers SET conclusion=?, conclusion_remark=?, updated_at=?
                   WHERE id=?""",
                (conclusion, remark, utc_now(), worker_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("伤者不存在")
        return self.get_worker(worker_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
