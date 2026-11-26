"""回退时段写闸夹具（硬闸口径）。"""
from threading import Thread

from fastapi.testclient import TestClient

from app.db import db as db_ctx

APP2 = 2  # 种子中无在途发版单的应用，便于走完整晋升流程
API = "/api"


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


def roster_version(app_id: int, env: str):
    with db_ctx() as c:
        row = c.execute(
            "SELECT version FROM app_env_versions WHERE app_id=? AND env_id=?",
            (app_id, env_id(env)),
        ).fetchone()
        return row["version"] if row else None


def instance_rows(app_id: int, env: str):
    with db_ctx() as c:
        return [dict(r) for r in c.execute(
            "SELECT version, health AS 健康 FROM instances WHERE app_id=? AND env_id=? ORDER BY id",
            (app_id, env_id(env)),
        ).fetchall()]


def prod_release_status(client, rid):
    return client.get(f"{API}/发版单/{rid}").json()["状态"]


def rb_window_row(client, env: str):
    rows = client.get(f"{API}/回滚窗").json()
    return next(w for w in rows if w["环境"] == env)


def bucket(client):
    return client.get(f"{API}/对账").json()["回退时段桶"]


# ---------- 1. 时段开放：回退整笔成功，六处同命运 ----------
def test_open_window_rollback_atomic_success(client):
    rid = release_to_prod(client)
    assert roster_version(APP2, "产") == "2.0.0"

    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 200
    rc = resp.json()

    # 回执：时段、单、名册、实例、对账、审计一起变
    assert rc["成败"] == "成功"
    assert rc["拒因"] == ""
    assert rc["回退时段"]["可回退"] is True
    assert rc["发版单"]["状态"] == "已回滚"
    assert rc["名册目标版本"] == "0.8.5"
    assert rc["对账桶"]["窗口内成功回退"] >= 1
    assert rc["审计"]["kind"] == "回滚"

    # 发版单已回滚且目标版本改写为旧版
    assert prod_release_status(client, rid) == "已回滚"
    assert client.get(f"{API}/发版单/{rid}").json()["目标版本"] == "0.8.5"
    # 名册目标旧版
    assert roster_version(APP2, "产") == "0.8.5"
    # 整组实例都收口到旧版且探针绿，无红灯残留
    inst = instance_rows(APP2, "产")
    assert inst and all(i["version"] == "0.8.5" and i["健康"] == "探针绿" for i in inst)

    bk = bucket(client)
    assert bk["窗口内成功回退"] >= 1
    assert bk["时段外回退尝试"] == 0
    assert bk["时段外成功回退"] == 0  # 硬闸自证
    rec = client.get(f"{API}/对账").json()
    assert rec["发版统计"]["已回滚"] == 1


# ---------- 2. 时段关闭：整笔失败，零漂移，留可核痕迹 ----------
def test_closed_window_rollback_fails_fully(client):
    rid = release_to_prod(client)
    before_roster = roster_version(APP2, "产")
    before_inst = instance_rows(APP2, "产")

    toggle_rb(client, "产", "窗关")
    # 读侧与写侧同一判定
    assert rb_window_row(client, "产")["可回退"] is False

    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 400
    payload = resp.json()["detail"]
    assert "回退窗已关" in payload["拒因"]
    rc = payload["回执"]
    assert rc["成败"] == "失败"
    assert rc["回退时段"]["可回退"] is False
    assert rc["发版单"]["状态"] == "已放行"  # 单未进已回滚

    # 名册、实例零改动（没有半截漂移）
    assert roster_version(APP2, "产") == before_roster == "2.0.0"
    assert instance_rows(APP2, "产") == before_inst
    assert prod_release_status(client, rid) == "已放行"

    # 对账分桶：计入时段外尝试，绝不计成功
    bk = bucket(client)
    assert bk["时段外回退尝试"] >= 1
    assert bk["时段外成功回退"] == 0
    assert client.get(f"{API}/对账").json()["发版统计"]["已回滚"] == 0

    # 审计可核痕迹
    rej = client.get(f"{API}/审计", params={"类别": "回退拒因"}).json()
    assert rej and all(r["类别"] == "回退拒因" for r in rej)
    assert "环境=产" in rej[0]["备注"]


# ---------- 3. 双回退并发：只写出一个目标版本 ----------
def test_double_rollback_writes_single_target(client):
    from app.main import app as ship_app
    rid = release_to_prod(client)
    results = {}

    def worker(target: str, key: str):
        tc = TestClient(ship_app)  # 每线程独立 client
        resp = tc.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": target})
        results[key] = resp.status_code

    t1 = Thread(target=worker, args=("0.8.5", "a"))
    t2 = Thread(target=worker, args=("0.7.0", "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == [200, 409]

    with db_ctx() as c:
        rolled = c.execute("SELECT COUNT(*) AS c FROM releases WHERE id=? AND status='已回滚'", (rid,)).fetchone()["c"]
        assert rolled == 1
        versions = {r["version"] for r in c.execute(
            "SELECT version FROM instances WHERE app_id=? AND env_id=?", (APP2, env_id("产"))
        ).fetchall()}
        roster = c.execute(
            "SELECT version FROM app_env_versions WHERE app_id=? AND env_id=?", (APP2, env_id("产"))
        ).fetchone()["version"]
    # 实例与名册只有一个目标版本，不存在两个目标版本
    assert versions == {roster}
    assert bucket(client)["回退冲突"] >= 1


# ---------- 4. 半截分叉（名册已旧、实例仍新）整笔收口 ----------
def test_half_split_roster_old_instance_new_converges(client):
    rid = release_to_prod(client)  # 产：名册=实例=2.0.0
    # 人为制造半截：名册先改成旧版，实例仍停在新版
    with db_ctx() as c:
        c.execute(
            "UPDATE app_env_versions SET version='0.8.5' WHERE app_id=? AND env_id=?",
            (APP2, env_id("产")),
        )
    assert roster_version(APP2, "产") == "0.8.5"
    # 半截：名册已旧，但仍有停留在新版 2.0.0 的实例
    pre = {i["version"] for i in instance_rows(APP2, "产")}
    assert "2.0.0" in pre and "0.8.5" in pre

    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 200

    # 整笔收口：名册与全部实例统一到旧版，无残留新包、无红灯
    assert roster_version(APP2, "产") == "0.8.5"
    inst = instance_rows(APP2, "产")
    assert all(i["version"] == "0.8.5" and i["健康"] == "探针绿" for i in inst)
    assert prod_release_status(client, rid) == "已回滚"


# ---------- 5. 事务中途失败：整笔撤到操作前 ----------
def test_mid_failure_rolls_back_entire_tx(client, monkeypatch):
    from app.main import app as ship_app
    rid = release_to_prod(client)
    before_roster = roster_version(APP2, "产")
    before_inst = instance_rows(APP2, "产")

    def boom(*a, **k):
        raise RuntimeError("实例收口故障")

    monkeypatch.setattr("app.routers.rollback.converge_instances", boom)
    quiet = TestClient(ship_app, raise_server_exceptions=False)
    resp = quiet.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    assert resp.status_code == 500

    # 全部回到操作前：单、名册、实例均未改，也没有成功审计
    assert prod_release_status(client, rid) == "已放行"
    assert roster_version(APP2, "产") == before_roster == "2.0.0"
    assert instance_rows(APP2, "产") == before_inst
    with db_ctx() as c:
        assert c.execute("SELECT COUNT(*) AS c FROM audit_events WHERE kind='回滚'").fetchone()["c"] == 0


# ---------- 6. 读侧/写侧同一时段判定，随开闭翻转 ----------
def test_read_write_share_same_predicate(client):
    rid = release_to_prod(client)
    toggle_rb(client, "产", "窗关")
    assert rb_window_row(client, "产")["可回退"] is False
    assert client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"}).status_code == 400

    toggle_rb(client, "产", "窗开")
    assert rb_window_row(client, "产")["可回退"] is True
    assert client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"}).status_code == 200


# ---------- 7. 审计范围过滤 ----------
def test_audit_scope_filters(client):
    # 第一单：窗开成功回退（回滚类）
    rid = release_to_prod(client)
    client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})
    # 第二单：先关窗再回退，被硬拒（回退拒因类），窗口已打开供下一轮编排
    toggle_rb(client, "产", "窗关")
    rid2 = release_to_prod(client, version="3.0.0")
    reject = client.post(f"{API}/发版单/{rid2}/回滚", json={"目标版本": "0.8.5"})
    assert reject.status_code == 400

    rej = client.get(f"{API}/审计", params={"类别": "回退拒因"}).json()
    assert rej and all(r["类别"] == "回退拒因" for r in rej)
    ok = client.get(f"{API}/审计", params={"类别": "回滚"}).json()
    assert ok and all(r["类别"] == "回滚" for r in ok)
    kw = client.get(f"{API}/审计", params={"动作关键字": "时段外"}).json()
    assert kw and all("时段外" in r["动作"] for r in kw)
    scoped = client.get(f"{API}/审计", params={"关联编号": rid}).json()
    assert scoped and all(r["关联编号"] == rid for r in scoped)


# ---------- 8. 健康探针 + 对账名册/实例列 ----------
def test_health_probe_and_reconcile_columns(client):
    rid = release_to_prod(client)
    client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "0.8.5"})

    inst = client.get(f"{API}/实例").json()
    prod = [i for i in inst if i["环境"] == "产" and i["应用"] == "配置投递"]
    assert prod and all(i["版本"] == "0.8.5" for i in prod)
    iid = prod[0]["编号"]
    client.post(f"{API}/实例/{iid}/健康", json={"健康": "探针红"})

    rec = client.get(f"{API}/对账").json()
    row = next(r for r in rec["名册与实例"] if r["应用"] == "配置投递" and r["环境"] == "产")
    assert row["名册版本"] == "0.8.5"
    assert row["红灯数"] == 1
    assert "实例探针" in row and "探针红" in row["实例探针"]


# ---------- 9. 窗口开闭戳随切换落库 ----------
def test_window_toggle_timestamp(client):
    toggle_rb(client, "产", "窗关")
    w = rb_window_row(client, "产")
    assert w["状态"] == "窗关" and w["开闭戳"]
    toggle_rb(client, "产", "窗开")
    w2 = rb_window_row(client, "产")
    assert w2["状态"] == "窗开" and w2["可回退"] is True and w2["开闭戳"]


# ---------- 10. 空目标版本被拒，不触闸不漂移 ----------
def test_blank_target_rejected(client):
    rid = release_to_prod(client)
    resp = client.post(f"{API}/发版单/{rid}/回滚", json={"目标版本": "   "})
    assert resp.status_code == 400
    assert prod_release_status(client, rid) == "已放行"
