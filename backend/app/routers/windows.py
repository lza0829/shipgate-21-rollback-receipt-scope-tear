from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import db, rows_to_list
from app.services.common import audit, env_by_code, now_str, rollback_window_open
from app.services import rollback_scope_surface

router = APIRouter(tags=["窗口"])


class WindowToggle(BaseModel):
    环境: str
    状态: str


@router.get("/放行窗")
def list_release_windows():
    with db() as conn:
        return rows_to_list(
            conn.execute(
                """
                SELECT w.id AS 编号, e.code AS 环境, e.name AS 环境名,
                       w.open_at AS 开启, w.close_at AS 关闭, w.status AS 状态
                FROM release_windows w JOIN environments e ON e.id = w.env_id
                ORDER BY e.sort_order, w.id DESC
                """
            ).fetchall()
        )


@router.get("/回滚窗")
def list_rollback_windows():
    with db() as conn:
        rows = rows_to_list(
            conn.execute(
                """
                SELECT w.id AS 编号, e.id AS 环境编号, e.code AS 环境, e.name AS 环境名,
                       w.open_at AS 开启, w.close_at AS 关闭, w.status AS 状态,
                       w.toggled_at AS 开闭戳
                FROM rollback_windows w JOIN environments e ON e.id = w.env_id
                ORDER BY e.sort_order, w.id DESC
                """
            ).fetchall()
        )
        # 读侧「可回退」直接复用写侧唯一谓词，读侧展示与写侧卡闸同一判定，杜绝两套口径。
        for row in rows:
            row["可回退"] = rollback_window_open(conn, row.pop("环境编号"))
        return rows


@router.post("/放行窗/切换")
def toggle_release(body: WindowToggle):
    if body.状态 not in ("窗开", "窗关"):
        raise HTTPException(400, "状态仅允许窗开或窗关")
    with db() as conn:
        env = env_by_code(conn, body.环境)
        row = conn.execute(
            "SELECT id FROM release_windows WHERE env_id=? ORDER BY id DESC LIMIT 1",
            (env["id"],),
        ).fetchone()
        if not row:
            raise HTTPException(404, "无放行窗记录")
        conn.execute("UPDATE release_windows SET status=? WHERE id=?", (body.状态, row["id"]))
        audit(conn, "放行窗", row["id"], body.状态, body.环境)
        return {"环境": body.环境, "状态": body.状态, "时间": now_str()}


@router.post("/回滚窗/切换")
def toggle_rollback(body: WindowToggle):
    if body.状态 not in ("窗开", "窗关"):
        raise HTTPException(400, "状态仅允许窗开或窗关")
    with db() as conn:
        env = env_by_code(conn, body.环境)
        row = conn.execute(
            "SELECT id FROM rollback_windows WHERE env_id=? ORDER BY id DESC LIMIT 1",
            (env["id"],),
        ).fetchone()
        if not row:
            raise HTTPException(404, "无回滚窗记录")
        now = now_str()
        conn.execute(
            "UPDATE rollback_windows SET status=?, toggled_at=? WHERE id=?",
            (body.状态, now, row["id"]),
        )
        audit(conn, "回滚窗", row["id"], body.状态, body.环境)
        return {"环境": body.环境, "状态": body.状态, "时间": now}

def _window_probe_rollback21(conn=None):
    if conn is None:
        return {"tag": "rollback21", "ready": True}
    try:
        return {"tag": "rollback21", "ok": rollback_scope_surface.get_setting(conn, "surface_ok", "0") == "1"}
    except Exception:
        return {"tag": "rollback21", "ready": True}

