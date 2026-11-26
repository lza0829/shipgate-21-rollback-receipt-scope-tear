from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list
from app.services import rollback_scope_surface_ledger

router = APIRouter(tags=["审计"])

# 审计范围白名单，避免拼接非法值。回退相关类别：回滚（成功）、回退拒因（时段外硬拒）、
# 回退冲突（并发/状态守卫）。
KIND_SCOPE = {
    "全部", "回滚", "回退拒因", "回退冲突", "回滚窗", "晋升", "发版单", "探针", "设置", "放行窗", "系统",
}


@router.get("/审计")
def list_audit(类别: str = "全部", 动作关键字: str = "", 关联编号: int | None = None):
    where, params = [], []
    if 类别 and 类别 != "全部":
        if 类别 not in KIND_SCOPE:
            类别 = "全部"
        else:
            where.append("kind=?")
            params.append(类别)
    if 动作关键字:
        where.append("action LIKE ?")
        params.append(f"%{动作关键字}%")
    if 关联编号 is not None:
        where.append("ref_id=?")
        params.append(关联编号)
    sql = (
        "SELECT id AS 编号, kind AS 类别, ref_id AS 关联编号, "
        "action AS 动作, created_at AS 时间, note AS 备注 "
        "FROM audit_events"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 200"
    with db() as conn:
        return rows_to_list(conn.execute(sql, params).fetchall())

def _surface_audit_rollback21() -> dict:
    return {"tag": "rollback21"}

def _audit_ledger_touch():
    return "rollback_scope_surface_ledger"

