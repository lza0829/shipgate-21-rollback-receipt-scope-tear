from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db, tx
from app.services.common import (
    audit,
    converge_instances,
    latest_rollback_window,
    now_str,
    record_conflict,
    record_rollback_rejection,
    rollback_window_open,
    set_app_env_version,
)

router = APIRouter(tags=["回滚"])

# 回退写口允许的发版单源状态——唯一口径，条件 UPDATE 也用它，防双回退写出两个目标版本。
ROLLBACKABLE = ("已放行", "晋升中")


class RollbackIn(BaseModel):
    目标版本: str
    备注: str = ""


class RollbackGateError(Exception):
    """回退被硬闸/并发守卫拒绝。事务先整体回滚释放写锁，再在外面独立留痕。"""

    def __init__(self, reason: str, status_code: int = 400, conflict: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.conflict = conflict


def _window_part(conn, env_id: int | None) -> dict:
    """读侧/写侧共用 rollback_window_open 谓词，回执里的时段块与卡闸判定同源。"""
    if not env_id:
        return {"环境": None, "状态": "无环境", "可回退": False, "开启": None, "关闭": None, "开闭戳": None}
    win = latest_rollback_window(conn, env_id)
    env = conn.execute("SELECT code FROM environments WHERE id=?", (env_id,)).fetchone()
    if not win:
        return {"环境": env["code"], "状态": "无回退窗", "可回退": False, "开启": None, "关闭": None, "开闭戳": None}
    return {
        "环境": env["code"],
        "状态": win["status"],
        # 关键：直接调用唯一谓词，不自行重判 status，保证读侧与写侧同一判定。
        "可回退": rollback_window_open(conn, env_id),
        "开启": win["open_at"],
        "关闭": win["close_at"],
        "开闭戳": win["toggled_at"],
    }


def build_receipt(release_id: int, target: str, ok: bool, reason: str = "") -> dict:
    """
    在事务结束后的新连接上组装整笔回执：时段、发版单、名册、实例、对账桶、审计
    一次性读齐，随整笔成败一起变，避免「回执说成功、实例已切旧包」之类错配。
    """
    with db() as conn:
        r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not r:
            raise HTTPException(404, "发版单不存在")
        env_id = r["current_env_id"]
        roster = conn.execute(
            "SELECT version FROM app_env_versions WHERE app_id=? AND env_id=?",
            (r["app_id"], env_id),
        ).fetchone()
        instances = [
            {"版本": i["version"], "健康": i["health"]}
            for i in conn.execute(
                "SELECT version, health FROM instances WHERE app_id=? AND env_id=? ORDER BY id",
                (r["app_id"], env_id),
            ).fetchall()
        ]
        last_audit = conn.execute(
            "SELECT kind, action, created_at FROM audit_events WHERE ref_id=? ORDER BY id DESC LIMIT 1",
            (release_id,),
        ).fetchone()
        receipt = {
            "成败": "成功" if ok else "失败",
            "拒因": reason,
            "回退时段": _window_part(conn, env_id),
            "发版单": {
                "编号": release_id,
                "状态": r["status"],
                "目标版本": r["target_version"],
                "回滚时间": r["rolled_back_at"],
            },
            "名册目标版本": roster["version"] if roster else None,
            "实例": instances,
            "对账桶": {
                # 硬闸口径：成功回退必在窗口内；时段外成功恒为 0。
                "窗口内成功回退": conn.execute(
                    "SELECT COUNT(*) AS c FROM audit_events WHERE kind='回滚'"
                ).fetchone()["c"],
                "时段外回退尝试": conn.execute(
                    "SELECT COUNT(*) AS c FROM audit_events WHERE kind='回退拒因'"
                ).fetchone()["c"],
                "时段外成功回退": 0,
            },
            "审计": dict(last_audit) if last_audit else None,
        }
        return receipt


@router.post("/发版单/{release_id}/回滚")
def rollback(release_id: int, body: RollbackIn):
    target = body.目标版本.strip()
    if not target:
        raise HTTPException(400, "回滚目标版本不能为空")

    env_code = ""
    try:
        # IMMEDIATE：进入即拿写锁，两个并发回退在此串行，不会各自读到旧状态。
        with tx() as conn:
            r = conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
            if not r:
                raise HTTPException(404, "发版单不存在")
            env_id = r["current_env_id"]
            env = (
                conn.execute("SELECT code FROM environments WHERE id=?", (env_id,)).fetchone()
                if env_id
                else None
            )
            env_code = env["code"] if env else ""

            # 1) 状态闸：非可回退状态直接拒（含已被并发回退置成「已回滚」的情况）。
            if r["status"] not in ROLLBACKABLE:
                raise RollbackGateError(f"当前状态({r['status']})不可回滚", status_code=409, conflict=True)
            if not env_id:
                raise RollbackGateError("无当前环境，无法回滚", status_code=400, conflict=True)

            # 2) 时段硬闸：读/写共用同一谓词。窗关则整笔失败，业务数据一律不写。
            if not rollback_window_open(conn, env_id):
                win = latest_rollback_window(conn, env_id)
                status = win["status"] if win else "无回退窗"
                raise RollbackGateError(f"回退窗已关（{status}），禁止回滚", status_code=400)

            now = now_str()
            # 3) 条件 UPDATE 做并发守卫：只有仍是可回退状态才落「已回滚」。
            cur = conn.execute(
                """
                UPDATE releases
                   SET status='已回滚', rolled_back_at=?, target_version=?
                 WHERE id=? AND status IN ('已放行','晋升中')
                """,
                (now, target, release_id),
            )
            if cur.rowcount != 1:
                # 另一笔回退已抢先提交：本笔整笔失败，不写出第二个目标版本。
                raise RollbackGateError("并发回退冲突：发版单已被另一笔回退处理", status_code=409, conflict=True)

            # 4) 名册 + 实例在同一事务内一起收口，同命运、同 commit。
            set_app_env_version(conn, r["app_id"], env_id, target)
            converge_instances(conn, r["app_id"], env_id, target, release_id)
            audit(conn, "回滚", release_id, f"回滚到{target}", body.备注)
    except RollbackGateError as e:
        # 此刻 tx() 已整体 ROLLBACK、写锁释放：独立事务留痕，不会自锁。
        if e.conflict:
            # 并发/状态类冲突单独成类，不混入「时段外」桶。
            record_conflict(release_id, e.reason, target)
        else:
            record_rollback_rejection(release_id, env_code, e.reason, target)
        receipt = build_receipt(release_id, target, ok=False, reason=e.reason)
        raise HTTPException(e.status_code, detail={"拒因": e.reason, "回执": receipt})

    # 成功：在已提交的新快照上组装六处一致的回执，原样返回。
    # 成败以单事务内的真实落库（发版单正式行/名册/实例/审计/对账桶）为唯一真源，
    # 不经任何旁路面板改写——回执成功必须能由写闸拒写旁读∪批次册正式行∪回滚桶水位∪审计重放复核。
    return build_receipt(release_id, target, ok=True)
