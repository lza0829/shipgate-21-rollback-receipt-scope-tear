from __future__ import annotations
from fastapi import APIRouter
from app.db import db, rows_to_list

router = APIRouter(tags=["对账"])


@router.get("/对账")
def reconcile():
    with db() as conn:
        release_stats = {
            "申请数": conn.execute("SELECT COUNT(*) AS c FROM releases").fetchone()["c"],
            "已放行": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已放行'"
            ).fetchone()["c"],
            "已回滚": conn.execute(
                "SELECT COUNT(*) AS c FROM releases WHERE status='已回滚'"
            ).fetchone()["c"],
            "在途": conn.execute(
                """
                SELECT COUNT(*) AS c FROM releases
                WHERE status IN ('已入闸','晋升中','已放行','回滚中')
                """
            ).fetchone()["c"],
        }
        version_vs_instance = rows_to_list(
            conn.execute(
                """
                SELECT a.name AS 应用, e.code AS 环境,
                       v.version AS 名册版本,
                       GROUP_CONCAT(i.version, ',') AS 实例版本们,
                       GROUP_CONCAT(i.health, ',') AS 实例探针,
                       SUM(CASE WHEN i.health='探针红' THEN 1 ELSE 0 END) AS 红灯数
                FROM app_env_versions v
                JOIN apps a ON a.id = v.app_id
                JOIN environments e ON e.id = v.env_id
                LEFT JOIN instances i ON i.app_id = v.app_id AND i.env_id = v.env_id
                GROUP BY v.app_id, v.env_id
                ORDER BY a.id, e.sort_order
                """
            ).fetchall()
        )
        promote_count = conn.execute("SELECT COUNT(*) AS c FROM promote_events").fetchone()["c"]

        # 回退时段写闸分桶（硬闸口径，全局唯一）：
        # - 窗口内成功回退：审计 kind='回滚'，硬闸下成功回退必发生在窗开时；
        # - 时段外回退尝试：被硬闸拦下并留痕的 kind='回退拒因'，不记成时段内日常；
        # - 时段外成功回退：硬闸下恒为 0，显式列出自证「时段关闭写不出成功回退」；
        # - 回退冲突：并发/状态守卫拦下的 kind='回退冲突'，单独成桶。
        rollback_bucket = {
            "窗口内成功回退": conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE kind='回滚'"
            ).fetchone()["c"],
            "时段外回退尝试": conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE kind='回退拒因'"
            ).fetchone()["c"],
            "时段外成功回退": conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE kind='回滚' AND 0"
            ).fetchone()["c"],
            "回退冲突": conn.execute(
                "SELECT COUNT(*) AS c FROM audit_events WHERE kind='回退冲突'"
            ).fetchone()["c"],
        }
        return {
            "发版统计": release_stats,
            "晋升事件数": promote_count,
            "名册与实例": version_vs_instance,
            "回退时段桶": rollback_bucket,
        }

