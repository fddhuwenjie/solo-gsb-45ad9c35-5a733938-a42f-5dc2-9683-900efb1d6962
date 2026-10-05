"""已完成会话计量表更正：提交、整段重核、修订号并发控制、查询与结算衔接、持久化。"""

import copy
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.corrections import submit_correction
from app.main import app
from app.storage import configure_store

client = TestClient(app)


DISPATCH = dict(
    prices=[0.0, 1.0, 1.0, 0.0],
    base_load_kw=[0.2, 0.2, 0.2, 0.2],
    pv_kw=[0.0, 0.0, 0.0, 0.0],
    capacity_kwh=0.4,
    max_charge_kw=1.0,
    max_discharge_kw=1.0,
    charge_efficiency=0.9,
    discharge_efficiency=0.9,
    initial_soc_kwh=0.0,
    min_terminal_soc_kwh=0.0,
    degradation_cost_per_kwh=0.2,
)

# 与原计划自洽的完整 4 段实测（计划：充 0.2、放 0.1、放 0.1、静置）。
RECORDS = [
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=round(0.2 / 0.9, 6), discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=round(0.1 + 0.2 / 0.9, 6),
        soc_start_kwh=0.0, soc_end_kwh=0.2,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.2, soc_end_kwh=0.1,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.09,
        grid_purchase_energy_kwh=0.01,
        soc_start_kwh=0.1, soc_end_kwh=0.0,
    ),
    dict(
        base_load_kw=0.2, pv_kw=0.0,
        charge_ac_energy_kwh=0.0, discharge_ac_energy_kwh=0.0,
        grid_purchase_energy_kwh=0.1,
        soc_start_kwh=0.0, soc_end_kwh=0.0,
    ),
]

TOL = 0.01


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """每个测试一个独立 SQLite 文件，避免用例间相互可见。"""
    configure_store(str(tmp_path / "sessions.db"))
    yield
    configure_store(None)


def _plan():
    return client.post("/dispatch", json=DISPATCH).json()


def _create(tol=TOL):
    r = client.post(
        "/dispatch/sessions",
        json={"dispatch": DISPATCH, "plan_snapshot": _plan(), "audit_tolerance_kwh": tol},
    )
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _append(sid, start_index, records):
    return client.post(
        f"/dispatch/sessions/{sid}/batches",
        json={"start_index": start_index, "records": records},
    )


def _complete(sid):
    """分两批把会话补齐到 complete。"""
    assert _append(sid, 0, RECORDS[:2]).status_code == 200
    assert _append(sid, 2, RECORDS[2:]).status_code == 200


def _correct(sid, expected_revision, entries):
    return client.post(
        f"/dispatch/sessions/{sid}/corrections",
        json={"expected_revision": expected_revision, "entries": entries},
    )


def _entry(index, **overrides):
    rec = copy.deepcopy(RECORDS[index])
    rec.update(overrides)
    return {"index": index, "record": rec}


def test_correction_requires_complete_session():
    """未完成会话不能提交/查询更正：session_not_complete，满期后放行。"""
    sid = _create()
    r = _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 422
    assert r.json()["error"] == "session_not_complete"
    g = client.get(f"/dispatch/sessions/{sid}/corrections")
    assert g.status_code == 422
    assert g.json()["error"] == "session_not_complete"

    assert _append(sid, 0, RECORDS[:2]).status_code == 200
    r = _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 422
    assert r.json()["error"] == "session_not_complete"

    assert _append(sid, 2, RECORDS[2:]).status_code == 200
    r = _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1


def test_correction_happy_path_and_query():
    """单条更正通过：修订号推进、差异下标正确、查询返回原始+修订+结算入参。"""
    sid = _create()
    _complete(sid)

    r = _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == sid
    assert body["revision"] == 1
    assert body["num_periods"] == 4
    assert body["corrected_indices"] == [1]
    assert body["changed_indices"] == [1]
    assert body["first_record_soc_gap_kwh"] == 0.0
    assert [a["index"] for a in body["revised_audit"]] == [0, 1, 2, 3]
    assert all(not a["anomalous"] for a in body["revised_audit"])
    assert body["stored_at"].endswith("Z")

    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 1
    assert d["changed_indices"] == [1]
    assert d["updated_at"] == body["stored_at"]
    # 原始记录保留最初上报值，修订记录为更正后的值
    assert d["original_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.01
    assert d["revised_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.015
    assert [r["index"] for r in d["original_records"]] == [0, 1, 2, 3]
    assert [r["index"] for r in d["revised_records"]] == [0, 1, 2, 3]
    assert d["revisions"] == [
        {"revision": 1, "corrected_indices": [1], "stored_at": body["stored_at"]}
    ]
    # 结算入参完整且 records 取当前修订
    sr = d["settlement_request"]
    assert sr["dispatch"]["capacity_kwh"] == 0.4
    assert sr["plan_snapshot"]["summary"]["num_periods"] == 4
    assert sr["settlement_tolerance_kwh"] == TOL
    assert sr["records"][1]["grid_purchase_energy_kwh"] == 0.015


def test_revision_zero_query_before_any_correction():
    """无更正的完成会话：第 0 版即原始记录，差异为空，结算入参用原始记录。"""
    sid = _create()
    _complete(sid)
    r = client.get(f"/dispatch/sessions/{sid}/corrections")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["current_revision"] == 0
    assert d["changed_indices"] == []
    assert d["revisions"] == []
    assert d["updated_at"] is None
    assert [r["record"] for r in d["revised_records"]] == [
        r["record"] for r in d["original_records"]
    ]
    assert d["settlement_request"]["records"][1]["grid_purchase_energy_kwh"] == 0.01
    assert all(not a["anomalous"] for a in d["revised_audit"])


def test_corrections_stack_on_current_version():
    """后续更正在当前版本上叠加：未指定时段保持当前（已修订）值。"""
    sid = _create()
    _complete(sid)

    r1 = _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r1.status_code == 200 and r1.json()["revision"] == 1

    # 第二次更正只动 t2：t1 的修订值 0.015 必须保留（叠加在当前版本上）
    r2 = _correct(sid, 1, [_entry(2, grid_purchase_energy_kwh=0.012)])
    assert r2.status_code == 200, r2.text
    assert r2.json()["revision"] == 2
    assert r2.json()["corrected_indices"] == [2]
    assert r2.json()["changed_indices"] == [1, 2]

    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 2
    assert d["changed_indices"] == [1, 2]
    assert d["revised_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.015
    assert d["revised_records"][2]["record"]["grid_purchase_energy_kwh"] == 0.012
    assert d["original_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.01
    assert d["original_records"][2]["record"]["grid_purchase_energy_kwh"] == 0.01
    assert [rev["revision"] for rev in d["revisions"]] == [1, 2]
    assert d["revisions"][0]["corrected_indices"] == [1]
    assert d["revisions"][1]["corrected_indices"] == [2]


def test_multi_entry_correction_in_one_submission():
    """一次提交多个「下标+替换记录」：一并核对、一并生效。"""
    sid = _create()
    _complete(sid)
    entries = [
        _entry(1, grid_purchase_energy_kwh=0.015),
        _entry(3, grid_purchase_energy_kwh=0.105),
    ]
    r = _correct(sid, 0, entries)
    assert r.status_code == 200, r.text
    assert r.json()["corrected_indices"] == [1, 3]
    assert r.json()["changed_indices"] == [1, 3]
    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["revised_records"][3]["record"]["grid_purchase_energy_kwh"] == 0.105


def test_stale_revision_returns_conflict_with_current_number():
    """期望修订号过期（落后或超前）→ 409 conflict，原因给出当前号，已有修订不变。"""
    sid = _create()
    _complete(sid)

    # 尚未有任何更正时期望 1：过期（当前为 0）
    r = _correct(sid, 1, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    assert "当前修订号为 0" in r.json()["reason"]

    assert _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)]).status_code == 200

    # 更正生效后再用旧号 0 提交：过期（当前为 1）
    r = _correct(sid, 0, [_entry(2, grid_purchase_energy_kwh=0.012)])
    assert r.status_code == 409
    assert "当前修订号为 1" in r.json()["reason"]
    # 超前号同样冲突
    r = _correct(sid, 5, [_entry(2, grid_purchase_energy_kwh=0.012)])
    assert r.status_code == 409
    assert "当前修订号为 1" in r.json()["reason"]

    # 已有修订不变：仍只有第 1 版
    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 1
    assert d["changed_indices"] == [1]
    assert len(d["revisions"]) == 1


def test_duplicate_index_rejected_with_index_named():
    """同一时段下标在一次更正中出现多次 → 422 并点名下标，修订不变。"""
    sid = _create()
    _complete(sid)
    entries = [
        _entry(1, grid_purchase_energy_kwh=0.015),
        _entry(1, grid_purchase_energy_kwh=0.016),
    ]
    r = _correct(sid, 0, entries)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "correction_index_duplicate"
    assert "下标 1 重复" in body["reason"]
    assert client.get(f"/dispatch/sessions/{sid}/corrections").json()["current_revision"] == 0


def test_out_of_range_index_rejected_with_index_named():
    """下标越出计划范围 → 422 并点名下标与计划范围，修订不变。"""
    sid = _create()
    _complete(sid)
    r = _correct(sid, 0, [{"index": 4, "record": copy.deepcopy(RECORDS[0])}])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "correction_out_of_range"
    assert "下标 4" in body["reason"]
    assert "[0, 4)" in body["reason"]
    assert client.get(f"/dispatch/sessions/{sid}/corrections").json()["current_revision"] == 0


def test_energy_imbalance_rejected_with_index_named():
    """替换记录导致某时段电芯能量失衡 → 整次拒绝并点名下标，已有修订不变。"""
    sid = _create()
    _complete(sid)
    assert _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)]).status_code == 200

    # t1 末态改成 0.15 但放电量不变：电芯能量残差 0.05 超容差
    r = _correct(sid, 1, [_entry(1, soc_end_kwh=0.15, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "correction_audit_failed"
    assert "下标 1" in body["reason"]
    assert "电芯能量残差" in body["reason"]

    # 购电量失衡同样整次拒绝
    r = _correct(sid, 1, [_entry(2, grid_purchase_energy_kwh=0.2)])
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "correction_audit_failed"
    assert "下标 2" in body["reason"]
    assert "购电量残差" in body["reason"]

    # 已有修订不变：仍停在第 1 版
    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 1
    assert d["revised_records"][1]["record"]["soc_end_kwh"] == 0.1


def test_soc_discontinuity_rejected_with_index_named():
    """替换记录自身自洽但与相邻记录 SOC 断点 → 整次拒绝并点名断点下标。"""
    sid = _create()
    _complete(sid)
    # t1 少放 0.05kWh（自洽：末态 0.15、购电 0.055），但 t2 首态仍是 0.1 → 断点在下标 2
    r = _correct(
        sid,
        0,
        [_entry(1, discharge_ac_energy_kwh=0.045,
                grid_purchase_energy_kwh=0.055, soc_end_kwh=0.15)],
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "correction_audit_failed"
    assert "下标 2" in body["reason"]
    assert "SOC 不衔接" in body["reason"]
    assert client.get(f"/dispatch/sessions/{sid}/corrections").json()["current_revision"] == 0


def test_first_record_must_meet_initial_soc():
    """修订后首条首态未在容差内接原初始 SOC → correction_boundary_gap 整次拒绝。"""
    sid = _create()
    _complete(sid)
    # t0 首态改成 0.05（原初始 SOC 为 0.0），其余保持自洽
    r = _correct(
        sid,
        0,
        [_entry(0, soc_start_kwh=0.05, soc_end_kwh=0.25,
                charge_ac_energy_kwh=round(0.2 / 0.9, 6))],
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "correction_boundary_gap"
    assert "下标 0" in body["reason"]
    assert "原初始 SOC" in body["reason"]
    assert client.get(f"/dispatch/sessions/{sid}/corrections").json()["current_revision"] == 0


def test_correction_may_deviate_from_plan_if_self_consistent():
    """修订记录偏离计划但自洽（含 SOC 轨迹不同）仍接受：不做计划轨迹核对。"""
    sid = _create()
    _complete(sid)
    # t1 静置（不放电，SOC 保持 0.2，自洽）；t2 放 0.18kWh 交流把 SOC 放到 0.0，
    # 与 t3 首态衔接——整段自洽但与计划 SOC 轨迹明显不同
    entries = [
        _entry(1, discharge_ac_energy_kwh=0.0,
               grid_purchase_energy_kwh=0.1, soc_end_kwh=0.2),
        _entry(2, discharge_ac_energy_kwh=0.18,
               grid_purchase_energy_kwh=0.0, soc_start_kwh=0.2, soc_end_kwh=0.0),
    ]
    r = _correct(sid, 0, entries)
    assert r.status_code == 200, r.text
    assert r.json()["changed_indices"] == [1, 2]
    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["revised_records"][1]["record"]["soc_end_kwh"] == 0.2


def test_original_session_views_untouched_by_corrections():
    """更正不改写原始证据：会话查询、批次重试判定与首次回执仍用最初上报记录。"""
    sid = _create()
    first = _append(sid, 0, RECORDS[:2]).json()
    last = _append(sid, 2, RECORDS[2:]).json()

    assert _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)]).status_code == 200

    # 原会话查询仍是最初上报的记录
    d = client.get(f"/dispatch/sessions/{sid}").json()
    assert d["status"] == "complete"
    assert d["confirmed_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.01
    assert d["final_soc_kwh"] == 0.0

    # 批次重试判定仍按最初上报内容：原样重试两批都 deduped，回执冻结不变
    again = _append(sid, 0, copy.deepcopy(RECORDS[:2])).json()
    assert again["result"] == "deduped"
    for key in ("confirmed_periods", "next_index", "status", "batch_audit",
                "first_record_soc_gap_kwh", "final_soc_kwh"):
        assert again[key] == first[key], key
    again_last = _append(sid, 2, copy.deepcopy(RECORDS[2:])).json()
    assert again_last["result"] == "deduped"
    for key in ("confirmed_periods", "next_index", "status", "batch_audit", "final_soc_kwh"):
        assert again_last[key] == last[key], key

    # 用原会话查询的记录走结算：仍是原始台账口径
    sr = client.post(
        "/dispatch/settlement",
        json={
            "dispatch": d["dispatch"],
            "plan_snapshot": d["plan_snapshot"],
            "settlement_tolerance_kwh": TOL,
            "records": [r["record"] for r in d["confirmed_records"]],
        },
    )
    assert sr.status_code == 200
    assert sr.json()["summary"]["actual_energy_cost_yuan"] == 0.02


def test_settlement_request_feeds_settlement_end_to_end():
    """更正查询返回的结算入参可直接提交 /dispatch/settlement，按修订台账计费。"""
    sid = _create()
    _complete(sid)
    assert _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)]).status_code == 200

    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    sr = client.post("/dispatch/settlement", json=d["settlement_request"])
    assert sr.status_code == 200, sr.text
    body = sr.json()
    assert body["status"] == "settled"
    assert body["settled_periods"] == 4
    # t1 实测购电 0.015（修订值）× 电价 1 元 → 能量费 0.015，总计 0.025
    assert body["summary"]["actual_energy_cost_yuan"] == 0.025
    assert body["summary"]["energy_cost_delta_yuan"] == 0.005


def test_corrections_survive_restart(tmp_path):
    """进程重启（同路径重开存储）后修订仍可查询，并可按当前修订号继续更正。"""
    db_path = str(tmp_path / "persist.db")
    configure_store(db_path)
    sid = _create()
    _complete(sid)
    assert _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)]).status_code == 200

    configure_store(db_path)  # 模拟重启

    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 1
    assert d["changed_indices"] == [1]
    assert d["revised_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.015
    assert d["original_records"][1]["record"]["grid_purchase_energy_kwh"] == 0.01

    # 重启后继续更正：旧期望号冲突，当前号可叠加
    r = _correct(sid, 0, [_entry(2, grid_purchase_energy_kwh=0.012)])
    assert r.status_code == 409
    r = _correct(sid, 1, [_entry(2, grid_purchase_energy_kwh=0.012)])
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2

    configure_store(db_path)  # 再次重启仍保留两版修订
    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 2
    assert [rev["revision"] for rev in d["revisions"]] == [1, 2]


def test_concurrent_corrections_only_one_wins():
    """并发同期望号更正：恰好一份生效，其余 409，修订号只推进一次。"""
    sid = _create()
    _complete(sid)
    outcomes = []

    def worker(entry):
        try:
            r = submit_correction(sid, 0, [entry])
            outcomes.append(("accepted", r["revision"]))
        except Exception as exc:  # noqa: BLE001
            outcomes.append((getattr(exc, "code", "error"), None))

    e1 = _entry(1, grid_purchase_energy_kwh=0.015)
    e2 = _entry(1, grid_purchase_energy_kwh=0.016)
    t1 = threading.Thread(target=worker, args=(e1,))
    t2 = threading.Thread(target=worker, args=(e2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(o[0] for o in outcomes) == ["accepted", "conflict"]
    assert [o[1] for o in outcomes if o[0] == "accepted"] == [1]
    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 1
    assert len(d["revisions"]) == 1
    # 生效的一份与 accepted 的内容一致
    saved = d["revised_records"][1]["record"]["grid_purchase_energy_kwh"]
    assert saved in (0.015, 0.016)


def test_legacy_db_without_corrections_table(tmp_path):
    """旧库（无更正表）打开时自动补建：满期会话可直接查询第 0 版并继续更正。"""
    db_path = str(tmp_path / "legacy.db")
    plan = _plan()
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            dispatch_json TEXT NOT NULL,
            plan_snapshot_json TEXT NOT NULL,
            audit_tolerance_kwh REAL NOT NULL,
            num_periods INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE session_records (
            session_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            stored_at TEXT NOT NULL,
            PRIMARY KEY (session_id, idx)
        );
        """
    )
    sid = "legacy-complete-session"
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
        (sid, json.dumps(DISPATCH), json.dumps(plan), TOL, 4, "2026-01-01T00:00:00Z"),
    )
    for idx, rec in enumerate(RECORDS):
        conn.execute(
            "INSERT INTO session_records VALUES (?, ?, ?, ?)",
            (sid, idx, json.dumps(rec), "2026-01-01T00:00:01Z"),
        )
    conn.commit()
    conn.close()

    configure_store(db_path)  # 新版代码打开旧库：更正表自动补建为空表

    d = client.get(f"/dispatch/sessions/{sid}/corrections").json()
    assert d["current_revision"] == 0
    assert d["changed_indices"] == []
    assert len(d["original_records"]) == 4

    r = _correct(sid, 0, [_entry(1, grid_purchase_energy_kwh=0.015)])
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1


def test_invalid_correction_inputs_rejected():
    """空条目、负下标、非法测量值、未知会话等非法输入 → 422/404，修订不变。"""
    sid = _create()
    _complete(sid)

    r = _correct(sid, 0, [])
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    r = client.post(
        f"/dispatch/sessions/{sid}/corrections",
        json={"expected_revision": 0,
              "entries": [{"index": -1, "record": RECORDS[0]}]},
    )
    assert r.status_code == 422

    r = _correct(sid, -1, [_entry(1)])
    assert r.status_code == 422

    bad = _entry(1, soc_end_kwh=0.5)  # 超容量 0.4
    r = _correct(sid, 0, [bad])
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    bad = _entry(1, base_load_kw=-0.1)
    r = _correct(sid, 0, [bad])
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_input"

    assert _correct("nope", 0, [_entry(1)]).status_code == 404
    assert client.get("/dispatch/sessions/nope/corrections").status_code == 404

    # 全部拒绝后修订号不变
    assert client.get(f"/dispatch/sessions/{sid}/corrections").json()["current_revision"] == 0


def test_existing_endpoints_unchanged():
    """新增更正能力不影响既有接口。"""
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.post("/dispatch", json=DISPATCH)
    assert r.status_code == 200 and len(r.json()["periods"]) == 4
    r = client.post(
        "/telemetry/audit",
        json={
            "capacity_kwh": DISPATCH["capacity_kwh"],
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.9,
            "energy_tolerance_kwh": TOL,
            "records": RECORDS,
        },
    )
    assert r.status_code == 200 and r.json()["anomalous"] is False
