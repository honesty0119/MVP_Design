from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "leads.db"
SCHEMA_PATH = ROOT / "schema.sql"
STATIC_PATH = ROOT / "static"
UTC8 = timezone(timedelta(hours=8))

ALLOWED_TRANSITIONS = {
    "NEW": {"PENDING_CALL", "INVALID"},
    "PENDING_CALL": {"CONNECTED", "UNREACHED", "INVALID"},
    "UNREACHED": {"PENDING_CALL", "INVALID"},
    "CONNECTED": {"VALID", "INVALID"},
    "VALID": {"PENDING_WECHAT", "MQL", "INVALID"},
    "PENDING_WECHAT": {"WECHAT_ADDED", "VALID", "INVALID"},
    "WECHAT_ADDED": {"MQL", "INVALID"},
    "MQL": {"SQL", "INVALID"},
    "SQL": set(),
    "INVALID": {"NEW"},  # 仅允许主管式“重新打开”，Demo 中要求填写原因
}


def now_iso() -> str:
    return datetime.now(UTC8).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    with db() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO users(name, role) VALUES (?, ?)",
                [("陈晨", "sales"), ("林岚", "sales"), ("王主管", "manager")],
            )
        if conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0:
            seed_leads(conn)


def seed_leads(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC8).replace(microsecond=0)
    samples = [
        ("13800001001", "信息流", "抖音-暑期营", "NEW", None, 2),
        ("13800001002", "搜索", "百度-品牌词", "PENDING_CALL", None, 1),
        ("13800001003", "信息流", "小红书-资料包", "UNREACHED", None, 2),
        ("13800001004", "活动", "上海线下沙龙", "VALID", None, 1),
        ("13800001005", "自然流量", "官网表单", "WECHAT_ADDED", None, 1),
        ("13800001006", "搜索", "百度-课程词", "MQL", None, 2),
        ("13800001007", "活动", "在线公开课", "SQL", None, 1),
        ("13800001008", "信息流", "抖音-低价课", "INVALID", "空号", 2),
        ("13800001009", "自然流量", "公众号菜单", "VALID", None, 2),
        ("13800001010", "活动", "合作方转介", "PENDING_WECHAT", None, 1),
    ]
    for idx, (phone, source, channel, status, invalid_reason, owner_id) in enumerate(samples):
        created = (now - timedelta(hours=idx * 13)).isoformat()
        last_follow = None if idx in (3, 8) else (now - timedelta(hours=idx * 5)).isoformat()
        cur = conn.execute(
            """INSERT INTO leads(phone, source, channel, status, invalid_reason,
               owner_id, created_at, updated_at, last_follow_up_at, mql_at, sql_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                phone,
                source,
                channel,
                status,
                invalid_reason,
                owner_id,
                created,
                created,
                last_follow,
                created if status in {"MQL", "SQL"} else None,
                created if status == "SQL" else None,
            ),
        )
        lead_id = cur.lastrowid
        conn.execute(
            "INSERT INTO status_history(lead_id, from_status, to_status, changed_at, note) VALUES (?, ?, ?, ?, ?)",
            (lead_id, None, status, created, "模拟数据初始化"),
        )


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def list_leads(params: dict[str, list[str]]) -> list[dict]:
    clauses, values = [], []
    for key in ("status", "source", "owner_id"):
        value = params.get(key, [""])[0]
        if value:
            clauses.append(f"l.{key} = ?")
            values.append(value)
    search = params.get("q", [""])[0].strip()
    if search:
        clauses.append("(l.phone LIKE ? OR l.channel LIKE ?)")
        values.extend([f"%{search}%", f"%{search}%"])
    if params.get("overdue", [""])[0] == "1":
        clauses.append(
            "l.status NOT IN ('SQL','INVALID') "
            "AND COALESCE(l.last_follow_up_at, l.created_at) < ?"
        )
        values.append((datetime.now(UTC8) - timedelta(hours=48)).isoformat())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"""SELECT l.*, u.name owner_name,
            CASE WHEN l.status NOT IN ('SQL','INVALID')
              AND COALESCE(l.last_follow_up_at, l.created_at) < ? THEN 1 ELSE 0 END overdue
            FROM leads l LEFT JOIN users u ON u.id=l.owner_id
            {where} ORDER BY l.updated_at DESC, l.id DESC""",
            [(datetime.now(UTC8) - timedelta(hours=48)).isoformat(), *values],
        ).fetchall()
    return [dict(r) for r in rows]


def lead_detail(lead_id: int) -> dict | None:
    with db() as conn:
        lead = row_dict(
            conn.execute(
                "SELECT l.*, u.name owner_name FROM leads l LEFT JOIN users u ON u.id=l.owner_id WHERE l.id=?",
                (lead_id,),
            ).fetchone()
        )
        if not lead:
            return None
        lead["follow_ups"] = [
            dict(r)
            for r in conn.execute(
                "SELECT f.*, u.name operator_name FROM follow_ups f LEFT JOIN users u ON u.id=f.operator_id WHERE lead_id=? ORDER BY created_at DESC",
                (lead_id,),
            )
        ]
        lead["history"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM status_history WHERE lead_id=? ORDER BY changed_at DESC, id DESC",
                (lead_id,),
            )
        ]
        return lead


def create_lead(data: dict, conn: sqlite3.Connection | None = None) -> tuple[int, bool]:
    phone = str(data.get("phone", "")).strip()
    if not (phone.isdigit() and len(phone) == 11):
        raise ValueError("手机号须为 11 位数字（仅使用模拟号码）")
    source = str(data.get("source", "")).strip()
    channel = str(data.get("channel", "")).strip()
    if not source or not channel:
        raise ValueError("来源和渠道不能为空")
    owns = conn is None
    conn = conn or connect()
    try:
        existing = conn.execute("SELECT id FROM leads WHERE phone=?", (phone,)).fetchone()
        if existing:
            return existing["id"], False
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO leads(phone, source, channel, status, owner_id, created_at, updated_at) VALUES (?, ?, ?, 'NEW', ?, ?, ?)",
            (phone, source, channel, data.get("owner_id") or None, ts, ts),
        )
        conn.execute(
            "INSERT INTO status_history(lead_id, from_status, to_status, changed_at, note) VALUES (?, NULL, 'NEW', ?, '新建线索')",
            (cur.lastrowid, ts),
        )
        if owns:
            conn.commit()
        return cur.lastrowid, True
    finally:
        if owns:
            conn.close()


def update_status(lead_id: int, data: dict) -> None:
    target = data.get("status")
    note = str(data.get("note", "")).strip()
    with db() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise LookupError("线索不存在")
        current = lead["status"]
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"不允许从 {current} 直接流转到 {target}")
        if target == "INVALID" and not data.get("invalid_reason"):
            raise ValueError("标记无效必须填写无效原因")
        if current == "INVALID" and target == "NEW" and len(note) < 4:
            raise ValueError("重新打开无效线索必须说明原因")
        if target in {"MQL", "SQL"} and not lead["owner_id"]:
            raise ValueError("MQL/SQL 必须先分配负责人")
        if target == "MQL" and not lead["last_follow_up_at"]:
            raise ValueError("标记 MQL 前至少需要一条跟进记录")
        if target == "SQL":
            if current != "MQL":
                raise ValueError("SQL 必须由 MQL 转化")
            if not data.get("opportunity_note") or len(str(data["opportunity_note"]).strip()) < 4:
                raise ValueError("标记 SQL 必须填写明确商机说明")
        ts = now_iso()
        conn.execute(
            """UPDATE leads SET status=?, updated_at=?, invalid_reason=?,
               mql_at=CASE WHEN ?='MQL' THEN ? ELSE mql_at END,
               sql_at=CASE WHEN ?='SQL' THEN ? ELSE sql_at END WHERE id=?""",
            (
                target,
                ts,
                data.get("invalid_reason") if target == "INVALID" else None,
                target,
                ts,
                target,
                ts,
                lead_id,
            ),
        )
        conn.execute(
            "INSERT INTO status_history(lead_id, from_status, to_status, changed_at, note) VALUES (?, ?, ?, ?, ?)",
            (lead_id, current, target, ts, note or data.get("opportunity_note")),
        )


def funnel() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) count FROM leads GROUP BY status").fetchall()
        counts = {r["status"]: r["count"] for r in rows}
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        valid = sum(counts.get(s, 0) for s in ("VALID", "PENDING_WECHAT", "WECHAT_ADDED", "MQL", "SQL"))
        mql = counts.get("MQL", 0) + counts.get("SQL", 0)
        sql = counts.get("SQL", 0)
        overdue = conn.execute(
            """SELECT COUNT(*) FROM leads WHERE status NOT IN ('SQL','INVALID')
               AND COALESCE(last_follow_up_at, created_at) < ?""",
            ((datetime.now(UTC8) - timedelta(hours=48)).isoformat(),),
        ).fetchone()[0]
    rate = lambda a, b: round(a * 100 / b, 1) if b else 0
    return {
        "counts": counts,
        "total": total,
        "valid_rate": rate(valid, total),
        "mql_rate": rate(mql, valid),
        "sql_rate": rate(sql, mql),
        "overdue": overdue,
    }


def natural_language_query(question: str) -> dict:
    """Small, explainable rule-query layer; deliberately does not pretend to be an LLM."""
    q = question.strip()
    if not q:
        raise ValueError("请输入问题")
    with db() as conn:
        if "SQL" in q.upper() and ("渠道" in q or "来源" in q) and any(
            word in q for word in ("最高", "最好", "最多")
        ):
            rows = conn.execute(
                """SELECT channel,
                   SUM(CASE WHEN status='SQL' THEN 1 ELSE 0 END) sql_count,
                   SUM(CASE WHEN status IN ('MQL','SQL') THEN 1 ELSE 0 END) qualified_count
                   FROM leads GROUP BY channel
                   HAVING qualified_count > 0
                   ORDER BY 1.0 * sql_count / qualified_count DESC, qualified_count DESC
                   LIMIT 5"""
            ).fetchall()
            if not rows:
                return {"answer": "当前没有进入 MQL/SQL 的线索，无法计算。", "rows": []}
            result = [dict(r) for r in rows]
            best = result[0]
            rate = round(best["sql_count"] * 100 / best["qualified_count"], 1)
            return {
                "answer": f"{best['channel']} 的 SQL 转化率最高，为 {rate}%。",
                "definition": "SQL 转化率 = SQL 数 /（MQL + SQL 数），按当前存量快照计算。",
                "rows": result,
            }
        if "超时" in q or ("48" in q and "跟进" in q):
            count = funnel()["overdue"]
            return {
                "answer": f"当前有 {count} 条线索超过 48 小时未跟进。",
                "definition": "排除 SQL/无效；按最近跟进时间，无跟进则按创建时间。",
                "action": {"label": "查看超时线索", "filter": "overdue"},
            }
        if "漏斗" in q or "各状态" in q:
            data = funnel()
            return {
                "answer": "已返回当前线索漏斗快照。",
                "definition": "这是当前存量快照，不是按进入周期计算的 cohort 漏斗。",
                "rows": [{"status": k, "count": v} for k, v in data["counts"].items()],
            }
    return {
        "answer": "当前规则查询暂不支持这个问题。",
        "suggestions": [
            "哪个渠道 SQL 转化率最高？",
            "当前有多少条线索跟进超时？",
            "查看各状态漏斗",
        ],
    }


def process_call_callback(data: dict) -> dict:
    """Persist a third-party call event once and apply only a legal call-state change."""
    event_id = str(data.get("event_id", "")).strip()
    lead_id = data.get("lead_id")
    result = str(data.get("result", "")).upper()
    if not event_id:
        raise ValueError("event_id 不能为空")
    if result not in {"CONNECTED", "UNREACHED"}:
        raise ValueError("result 仅支持 CONNECTED 或 UNREACHED")
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO callback_events(event_id, event_type, payload, received_at) VALUES (?, 'call', ?, ?)",
                (event_id, json.dumps(data, ensure_ascii=False), now_iso()),
            )
        except sqlite3.IntegrityError:
            return {"ok": True, "duplicate": True, "status_changed": False}
        lead = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            raise LookupError("线索不存在")
        current = lead["status"]
        # 回调只负责外呼事实；若人工已推进到后续阶段，不允许旧回调把状态拉回。
        if current != "PENDING_CALL":
            return {
                "ok": True,
                "duplicate": False,
                "status_changed": False,
                "reason": f"当前状态 {current} 不接受外呼回调",
            }
        ts = now_iso()
        conn.execute(
            "UPDATE leads SET status=?, updated_at=? WHERE id=?",
            (result, ts, lead_id),
        )
        conn.execute(
            "INSERT INTO status_history(lead_id, from_status, to_status, changed_at, note) VALUES (?, ?, ?, ?, ?)",
            (lead_id, current, result, ts, f"外呼系统回调 event_id={event_id}"),
        )
    return {"ok": True, "duplicate": False, "status_changed": True}


class Handler(SimpleHTTPRequestHandler):
    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def _route(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, params = self._route()
        try:
            if path == "/api/leads":
                return self._json(list_leads(params))
            if path.startswith("/api/leads/"):
                item = lead_detail(int(path.rsplit("/", 1)[1]))
                return self._json(item or {"error": "线索不存在"}, HTTPStatus.OK if item else HTTPStatus.NOT_FOUND)
            if path == "/api/funnel":
                return self._json(funnel())
            if path == "/api/users":
                with db() as conn:
                    return self._json([dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id")])
            if path == "/api/meta":
                with db() as conn:
                    sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM leads ORDER BY source")]
                return self._json({"sources": sources, "transitions": {k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()}})
            if path == "/api/export.csv":
                rows = list_leads(params)
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(["id", "phone", "source", "channel", "status", "owner_name", "created_at"])
                for r in rows:
                    writer.writerow([r[k] for k in ("id", "phone", "source", "channel", "status", "owner_name", "created_at")])
                body = out.getvalue().encode("utf-8-sig")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="leads.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return super().do_GET()
        except Exception as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        path, _ = self._route()
        try:
            data = self._body()
            if path == "/api/leads":
                lead_id, created = create_lead(data)
                return self._json({"id": lead_id, "created": created}, HTTPStatus.CREATED if created else HTTPStatus.OK)
            if path.endswith("/status") and path.startswith("/api/leads/"):
                lead_id = int(path.split("/")[3])
                update_status(lead_id, data)
                return self._json({"ok": True})
            if path.endswith("/assign") and path.startswith("/api/leads/"):
                lead_id = int(path.split("/")[3])
                with db() as conn:
                    if not conn.execute("SELECT 1 FROM users WHERE id=?", (data.get("owner_id"),)).fetchone():
                        raise ValueError("负责人不存在")
                    conn.execute("UPDATE leads SET owner_id=?, updated_at=? WHERE id=?", (data["owner_id"], now_iso(), lead_id))
                return self._json({"ok": True})
            if path.endswith("/follow-ups") and path.startswith("/api/leads/"):
                lead_id = int(path.split("/")[3])
                content = str(data.get("content", "")).strip()
                if len(content) < 2:
                    raise ValueError("跟进内容过短")
                ts = now_iso()
                with db() as conn:
                    lead = conn.execute("SELECT owner_id FROM leads WHERE id=?", (lead_id,)).fetchone()
                    if not lead:
                        raise LookupError("线索不存在")
                    operator = data.get("operator_id") or lead["owner_id"]
                    if not operator:
                        raise ValueError("请先分配负责人")
                    conn.execute(
                        "INSERT INTO follow_ups(lead_id, operator_id, content, next_action_at, created_at) VALUES (?, ?, ?, ?, ?)",
                        (lead_id, operator, content, data.get("next_action_at") or None, ts),
                    )
                    conn.execute("UPDATE leads SET last_follow_up_at=?, updated_at=? WHERE id=?", (ts, ts, lead_id))
                return self._json({"ok": True}, HTTPStatus.CREATED)
            if path == "/api/import":
                rows = data.get("rows", [])
                if not isinstance(rows, list) or len(rows) > 500:
                    raise ValueError("单次最多导入 500 条")
                created = duplicates = errors = 0
                details = []
                with db() as conn:
                    for i, row in enumerate(rows, 1):
                        try:
                            _, added = create_lead(row, conn)
                            created += int(added)
                            duplicates += int(not added)
                        except ValueError as exc:
                            errors += 1
                            details.append({"row": i, "error": str(exc)})
                return self._json({"created": created, "duplicates": duplicates, "errors": errors, "details": details})
            if path == "/api/query":
                return self._json(natural_language_query(str(data.get("question", ""))))
            if path == "/api/callback/call":
                return self._json(process_call_callback(data))
            return self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except LookupError as exc:
            return self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return self._json({"error": f"服务异常：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def translate_path(self, path):
        clean = urlparse(path).path
        if clean == "/":
            clean = "/index.html"
        return str(STATIC_PATH / clean.lstrip("/"))

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def serve(port: int = 8000):
    init_db()
    print(f"市场线索 MVP 已启动：http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        init_db(reset=True)
        print("数据库已重置。")
    else:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
        serve(port)
