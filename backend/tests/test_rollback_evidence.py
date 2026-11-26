"""假回滚成功收口回归夹具。

事故口径：成功回执必须能由「写闸拒写旁读 ∪ 批次册正式行 ∪ 回滚桶水位 ∪ 审计同命运重放」
四证复核，缺一则整笔退回回滚前；改成功提示不算收口。本文件在既有硬闸夹具之外，专门钉：
- 成功提示与批次正式行/水位/审计的同屏可核（防 surface 旁路把状态改回「已放行」而回执仍报成功）；
- 事件重放能揭穿假回滚回执；
- 回滚写口与放行（晋升）写口交错半截必须收束；
- surface 毒根（rollback21 旁路面板/遮蔽账本）不得复活。
"""
from threading import Thread

from fastapi.testclient import TestClient

from app.db import db as db_ctx

API = "/api"
APP2 = 2  # 种子中无在途发版单的应用


# ---------- 编排助手 ----------
def env_id(code: str) -> int:
    with db_ctx() as c:
        return c.execute("SELECT id FROM environments WHERE code=?", (code,)).fetchone()["id"]


def release_to_prod(client, app_id: int = APP2, version: str = "2.0.0") -> int:
    r = client.post(f"{API}/发版单", json={
        "应用编号": app_id, "目标版本": version, "制品摘要": "sha256:fixture",
    })
    rid = r.json()["编号"]
    client.post(f"{API}/发版单/{rid}/流水线", json={"流水线": "绿"})
    client.post(f"{API}/发版单/{rid}/入闸", json={})
    client.post(f"{API}/发版单/{rid}/晋升", json={"目标环境": "预"})
    client.post(f"{API}/发版单/{rid}/晋升", json={"目标环境": "产"})
    return rid


def toggle_rb(client, env: str, status: str):
    return client.post(f"{API}/回滚窗/切换", json={"环境": env, "状态": status})


def roster_version(client, app_id: int, env: str):
    with db_ctx() as c:
        row = c.execute(
            "SELECT version FROM app_env_versions WHERE app_id=? AND env_id=?",
            (app_id, env_id(env)),
        ).fetchone()
        return row["version"] if row else None


def instances(client, app_id: int, env: str):
    with db_ctx() as c:
        return [dict(r) for r in c.execute(
            "SELECT version, health AS 健康, release_id FROM instances WHERE app_id=? AND env_id=? ORDER BY id",
            (app_id, env_id(env)),
        ).fetchall()]


def db_status(rid: int) -> str:
    with db_ctx() as c:
        return c.execute("SELECT status FROM releases WHERE id=?", (rid,)).fetchone()["status"]


def audit_rows(client, rid: int):
    return client.get(f"{API}/审计", params={"关联编号": rid}).json()


def bucket(client):
    return client.get(f"{API}/对账").json()["回退时段桶"]


# ---------- 1. 成功四证同命运：回执成功 ⇒ 写闸旁读/批次正式行/水位/审计同时成立 ----------
def test_success_receipt_corroborated_by_four_evidences(client):
    rid = release_to_prod(client)
    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 200
    rc = resp.json()

    # 证一：回执自身不被旁路改写，状态就是落库的「已回滚」
    assert rc["成败"] == "成功"
    assert rc["发版单"]["状态"] == "已回滚"
    assert rc["回退时段"]["可回退"] is True

    # 证二：批次册正式行（发版单）可核——状态、目标旧版、回滚时间齐全，不缺行不回退
    detail = client.get(f"{API}/发版单/{rid}").json()
    assert detail["状态"] == "已回滚"
    assert detail["目标版本"] == "0.8.5"
    assert detail["回滚时间"]
    assert db_status(rid) == "已回滚"

    # 证三：总览回滚桶水位上涨（已回滚 +1，在途/已放行回落）
    dist = client.get(f"{API}/总览").json()["分布"]
    assert dist["已回滚"] >= 1
    assert dist["已放行"] == 0

    # 证四：对账窗口内成功桶与发版统计同步上涨，时段外成功恒 0
    rec = client.get(f"{API}/对账").json()
    assert rec["发版统计"]["已回滚"] >= 1
    bk = rec["回退时段桶"]
    assert bk["窗口内成功回退"] >= 1
    assert bk["时段外成功回退"] == 0

    # 名册与整组实例同屏对读：全部收口到目标旧版且探针绿，挂本发版单
    assert roster_version(client, APP2, "产") == "0.8.5"
    inst = instances(client, APP2, "产")
    assert inst and all(
        i["version"] == "0.8.5" and i["健康"] == "探针绿" and i["release_id"] == rid
        for i in inst
    )


# ---------- 2. 事件重放揭穿假回执：审计末条与回执、DB 状态严格一致，无伪造行 ----------
def test_event_replay_reveals_true_outcome(client):
    rid = release_to_prod(client)
    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    rc = resp.json()

    # 回执里的审计块与审计过滤接口、DB 审计表三处同一条「回滚」事件
    assert rc["审计"]["kind"] == "回滚"
    rows = audit_rows(client, rid)
    kinds = [r["类别"] for r in rows]
    assert kinds.count("回滚") == 1
    assert not any(r["类别"] in ("surface", "rollback21") for r in rows)
    with db_ctx() as c:
        db_kinds = [r["kind"] for r in c.execute(
            "SELECT kind FROM audit_events WHERE ref_id=?", (rid,)).fetchall()]
    assert db_kinds.count("回滚") == 1
    # 重放末条事件必须与当前真实状态吻合：事件说回滚，批次行就是已回滚
    assert db_status(rid) == "已回滚"


# ---------- 3. 窗关硬拒：缺写闸旁读即整笔退回，水位/对账不涨，留拒因，无假成功 ----------
def test_closed_window_full_rollback_no_false_success(client):
    rid = release_to_prod(client)
    before_roster = roster_version(client, APP2, "产")
    before_inst = instances(client, APP2, "产")
    toggle_rb(client, "产", "窗关")

    # 写闸拒新写旁读：读侧可回退=False，与写侧同一谓词
    win = next(w for w in client.get(f"{API}/回滚窗").json() if w["环境"] == "产")
    assert win["可回退"] is False

    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 400
    payload = resp.json()["detail"]
    rc = payload["回执"]
    # 失败回执不得伪装成功
    assert rc["成败"] == "失败"
    assert rc["发版单"]["状态"] == "已放行"

    # 整笔退回回滚前：批次行、名册、实例零漂移
    assert db_status(rid) == "已放行"
    assert roster_version(client, APP2, "产") == before_roster
    assert instances(client, APP2, "产") == before_inst

    # 缺口未清：回滚桶水位与对账不得上涨，只进时段外尝试
    dist = client.get(f"{API}/总览").json()["分布"]
    assert dist["已回滚"] == 0
    bk = bucket(client)
    assert bk["窗口内成功回退"] == 0
    assert bk["时段外回退尝试"] >= 1
    assert bk["时段外成功回退"] == 0
    assert client.get(f"{API}/对账").json()["发版统计"]["已回滚"] == 0

    # 审计留可核拒因，且失败回执审计块不谎报回滚成功
    rej = client.get(f"{API}/审计", params={"类别": "回退拒因"}).json()
    assert rej and rej[0]["关联编号"] == rid
    assert rc["审计"] is None or rc["审计"].get("kind") != "回滚"


# ---------- 4. 回滚写口与放行（晋升）写口交错半截：必须收束，不得放行已回滚单 ----------
def test_rollback_interleaved_with_promote_converges(client):
    rid = release_to_prod(client)
    # 先回滚成功
    assert client.post(
        f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"}
    ).status_code == 200
    assert db_status(rid) == "已回滚"

    # 交错：回滚已收口后，放行写口（晋升）不得再把这张已回滚单推进/重开
    resp = client.post(f"{API}/发版单/{rid}/晋升", json={"目标环境": "产"})
    assert resp.status_code == 400
    assert db_status(rid) == "已回滚"
    # 名册/实例仍停在回滚目标旧版，未被放行写口翻回新版
    assert roster_version(client, APP2, "产") == "0.8.5"
    assert all(i["version"] == "0.8.5" for i in instances(client, APP2, "产"))

    # 反向交错：晋升在途单（未到产）与回滚并发，回滚只接受「已放行/晋升中」且串行化，
    # 不出现状态拧花——回滚后该单稳定在已回滚，桶水位只计一次成功。
    rid2 = release_to_prod(client, version="4.0.0")
    client.post(f"{API}/发版单/{rid2}/晋升", json={"目标环境": "预"})  # 状态=晋升中
    assert client.post(
        f"{API}/发版单/{rid2}/回滚", json={"目标版本": "0.9.0"}
    ).status_code == 200
    assert db_status(rid2) == "已回滚"
    bk = bucket(client)
    assert bk["窗口内成功回退"] == 2


# ---------- 5. 双回滚并发：批次行只落一个正式行，证据不撕裂 ----------
def test_concurrent_double_rollback_single_book_line(client):
    from app.main import app as ship_app
    rid = release_to_prod(client)
    results = {}

    def worker(target: str, key: str):
        tc = TestClient(ship_app)
        results[key] = tc.post(
            f"{API}/发版单/{rid}/回滚", json={"目标版本": target}
        ).status_code

    t1 = Thread(target=worker, args=("0.8.5", "a"))
    t2 = Thread(target=worker, args=("0.7.0", "b"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results.values()) == [200, 409]

    # 批次正式行唯一且为已回滚；名册/实例只有一个目标版本
    with db_ctx() as c:
        row = c.execute("SELECT status, target_version FROM releases WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "已回滚"
        roster = c.execute(
            "SELECT version FROM app_env_versions WHERE app_id=? AND env_id=?",
            (APP2, env_id("产")),
        ).fetchone()["version"]
        versions = {r["version"] for r in c.execute(
            "SELECT version FROM instances WHERE app_id=? AND env_id=?",
            (APP2, env_id("产")),
        ).fetchall()}
    assert versions == {roster}
    assert bucket(client)["回退冲突"] >= 1


# ---------- 6. 跨环窗旁读：本环境窗关，他环境窗开不松闸 ----------
def test_cross_env_window_does_not_relax_gate(client):
    rid = release_to_prod(client)  # 落在产
    toggle_rb(client, "产", "窗关")
    toggle_rb(client, "预", "窗开")  # 预环境开着，不影响产
    win_prod = next(w for w in client.get(f"{API}/回滚窗").json() if w["环境"] == "产")
    assert win_prod["可回退"] is False
    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 400
    assert db_status(rid) == "已放行"
    toggle_rb(client, "产", "窗开")  # 复原供后续


# ---------- 7. 在途回滚格：回滚后从总览在途列表移除，进入已回滚水位 ----------
def test_rolled_back_leaves_inflight_grid(client):
    rid = release_to_prod(client)
    inflight_before = [r["编号"] for r in client.get(f"{API}/总览").json()["在途"]]
    assert rid in inflight_before
    client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    overview = client.get(f"{API}/总览").json()
    inflight_after = [r["编号"] for r in overview["在途"]]
    assert rid not in inflight_after
    assert overview["分布"]["已回滚"] >= 1


# ---------- 8. 毒根不复活：surface/rollback21 旁路模块、设置键、遮蔽账本全部消失 ----------
def test_surface_sabotage_root_not_revived(client):
    import importlib
    import app.services as services

    # 旁路模块不得被 import
    for name in (
        "app.services.rollback_scope_surface",
        "app.services.rollback_scope_surface_hooks",
        "app.services.rollback_scope_surface_ledger",
    ):
        assert importlib.util.find_spec(name) is None, f"{name} 毒模块复活"

    # 服务层不得再导出任何 surface/rollback21 符号
    assert not any("surface" in n.lower() or "rollback21" in n.lower()
                   for n in dir(services))

    # 一次真实回滚后，settings 里不得出现旁路面板键，审计不得出现伪造 surface 行
    rid = release_to_prod(client)
    client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    with db_ctx() as c:
        keys = [r["key"] for r in c.execute("SELECT key FROM settings").fetchall()]
        assert not any(k in ("surface_ok", "surface_last", "ledger_last") or
                       k.startswith("probe_") for k in keys)
        bad = c.execute(
            "SELECT COUNT(*) AS c FROM audit_events WHERE kind LIKE '%surface%' OR action LIKE '%surface%'"
        ).fetchone()["c"]
        assert bad == 0
