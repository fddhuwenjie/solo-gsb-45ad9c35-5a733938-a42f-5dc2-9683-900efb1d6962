"""已完成执行会话的计量表更正：保留原始上报证据，叠加修订后的实测台账。

规则（与执行会话、遥测审计同一套口径）：
1. 仅 status=complete 的会话可更正：完整原始台账已锁定，更正在其上叠加；
   未完成会话按 session_not_complete 拒绝；
2. 每次更正提交期望修订号与至少一个「时段下标 + 完整替换记录」；会话当前
   修订号从 0 开始（第 0 版即最初上报的原始记录），更正在当前版本上叠加，
   未指定时段保持当前版本的值；
3. 用会话的容量、效率、初始 SOC 与审计容差重新核对整段修订记录：首条首态
   须在容差内接原初始 SOC，逐条电芯能量守恒、购电量平衡、相邻记录 SOC 衔接；
   下标重复/越界、任一时段能量失衡或相邻 SOC 断点均整次拒绝，已有修订不变，
   原因中点名出错下标；
4. 期望修订号与当前号不一致（过期）→ 409 conflict，原因中给出当前修订号；
5. 核对通过后，新修订号（当前号 + 1）与全量修订记录在同一 SQLite 写事务内
   原子落库；并发更正由 BEGIN IMMEDIATE 串行判定，只有一份生效；
6. 原会话查询、批次重试判定与首次回执继续使用最初上报的记录
   （session_records / session_batches 不改写）；修订存于独立的
   session_corrections 表，进程重启后仍可查询并继续更正。

HTTP 错误码：
- session_not_found：会话不存在（404）；
- session_not_complete：会话尚未完成，无完整原始台账可更正（422）；
- invalid_input：条目为空、测量值为负/非有限、SOC 超容量等（422）；
- correction_index_duplicate：同一时段下标在一次更正中出现多次（422）；
- correction_out_of_range：下标超出计划时段范围 [0, num_periods)（422）；
- correction_boundary_gap：修订后首条首态未在容差内接原初始 SOC（422）；
- correction_audit_failed：修订后整段台账存在能量失衡或相邻 SOC 断点（422）；
- conflict：期望修订号过期（409，原因中给出当前修订号）。
"""

from __future__ import annotations

import json
from typing import List, Optional, Sequence, Tuple

from app.models import TelemetryRecord, _validate_telemetry_records
from app.sessions import (
    SessionError,
    _audit_range,
    _load_session,
    _records_to_models,
    _utc_now_iso,
)
from app.settlement import _round6
from app.storage import SessionStore, get_store


def _load_complete_session(
    store: SessionStore, session_id: str
) -> Tuple[dict, List[TelemetryRecord], list]:
    """载入会话并要求已完成：返回会话、最初上报的原始记录与对应存储行。"""
    session = _load_session(store, session_id)
    rows = store.get_all_records(session_id)
    num_periods = session["num_periods"]
    if len(rows) < num_periods:
        raise SessionError(
            "session_not_complete",
            f"会话 {session_id} 尚未完成（已确认 {len(rows)}/{num_periods} 段）："
            "计量表更正仅适用于已完成会话，请先按批次补齐实测记录",
        )
    return session, _records_to_models(rows), rows


def _canonical(records: Sequence[TelemetryRecord]) -> List[dict]:
    """与 session_records 入库口径一致的 canonical JSON（便于逐字段比对差异）。"""
    return [json.loads(r.model_dump_json()) for r in records]


def _changed_indices(original: Sequence[dict], revised: Sequence[dict]) -> List[int]:
    """当前修订与最初上报记录不一致的时段下标（升序）。"""
    return [i for i, (orig, rev) in enumerate(zip(original, revised)) if orig != rev]


def submit_correction(
    session_id: str,
    expected_revision: int,
    entries: Sequence[dict],
    store: Optional[SessionStore] = None,
) -> dict:
    """提交计量表更正：核对通过后原子保存新修订号与全量修订记录。

    更正在当前版本上叠加（未指定时段保持当前值）；任一核对失败整次拒绝，
    已有修订不变。期望修订号过期时抛 409 conflict 并给出当前修订号。
    """
    store = store or get_store()
    session, original_records, _rows = _load_complete_session(store, session_id)
    dispatch = session["dispatch"]
    tol = session["audit_tolerance_kwh"]
    num_periods = session["num_periods"]
    eta_c = dispatch["charge_efficiency"]
    eta_d = dispatch["discharge_efficiency"]

    if not entries:
        raise SessionError(
            "invalid_input", "更正条目不能为空：至少提交一个「时段下标 + 完整替换记录」"
        )

    # 逐条解析替换记录（有限/非负/SOC 不超容量，非法即 invalid_input）
    parsed: List[Tuple[int, TelemetryRecord]] = [
        (int(e["index"]), TelemetryRecord.model_validate(e["record"])) for e in entries
    ]
    _validate_telemetry_records([rec for _, rec in parsed], dispatch["capacity_kwh"])

    # 下标重复 / 越界：整次拒绝并点名出错下标
    seen = set()
    for idx, _rec in parsed:
        if idx in seen:
            raise SessionError(
                "correction_index_duplicate",
                f"更正条目下标 {idx} 重复：同一时段在一次更正中至多替换一次，"
                "整次更正不生效，已有修订不变",
            )
        seen.add(idx)
        if idx < 0 or idx >= num_periods:
            raise SessionError(
                "correction_out_of_range",
                f"更正条目下标 {idx} 超出计划范围 [0, {num_periods})："
                f"计划共 {num_periods} 个半小时时段，整次更正不生效，已有修订不变",
            )

    original_canonical = _canonical(original_records)
    stored_at = _utc_now_iso()

    with store.write_transaction() as conn:
        # 修订号判定与落库在同一写事务临界区内完成：并发更正只有一份生效
        current = store.get_current_revision(conn, session_id)
        if expected_revision != current:
            raise SessionError(
                "conflict",
                f"期望修订号 {expected_revision} 已过期：会话当前修订号为 {current}；"
                "已有修订不变，请基于当前修订重新提交更正",
                409,
            )

        # 基线＝当前版本的全量记录（第 0 版即最初上报的原始记录），
        # 未指定时段保持当前版本的值
        if current == 0:
            base = list(original_canonical)
        else:
            row = store.get_correction(conn, session_id, current)
            base = json.loads(row["records_json"])

        revised = list(base)
        for idx, rec in parsed:
            revised[idx] = json.loads(rec.model_dump_json())
        revised_models = [TelemetryRecord.model_validate(p) for p in revised]

        # 整段重核之一：首条首态须在审计容差内接原初始 SOC
        initial_soc = dispatch["initial_soc_kwh"]
        start_gap = revised_models[0].soc_start_kwh - initial_soc
        if abs(start_gap) > tol:
            raise SessionError(
                "correction_boundary_gap",
                f"修订后下标 0 的首态 SOC {revised_models[0].soc_start_kwh}kWh "
                f"与原初始 SOC {initial_soc}kWh 相差 {start_gap:+.6f}kWh，"
                f"超审计容差 {tol}kWh，整次更正不生效，已有修订不变",
            )

        # 整段重核之二：沿用遥测审计逐条核对电芯能量守恒、购电量平衡与相邻 SOC 衔接
        revised_audit = _audit_range(revised_models, 0, eta_c, eta_d, tol)
        bad = [a for a in revised_audit if a["anomalous"]]
        if bad:
            first_bad = bad[0]
            detail = "；".join(first_bad["reasons"])
            raise SessionError(
                "correction_audit_failed",
                f"修订后台账中下标 {first_bad['index']} 未通过遥测审计，"
                f"整次更正不生效，已有修订不变：{detail}",
            )

        # 全部通过：新修订号与全量修订记录在同一事务内原子落库
        new_revision = current + 1
        entries_json = json.dumps(
            [
                {"index": idx, "record": json.loads(rec.model_dump_json())}
                for idx, rec in parsed
            ],
            ensure_ascii=False,
        )
        store.insert_correction(
            conn,
            session_id,
            new_revision,
            json.dumps(revised, ensure_ascii=False),
            entries_json,
            stored_at,
        )

    return {
        "session_id": session_id,
        "revision": new_revision,
        "num_periods": num_periods,
        "corrected_indices": sorted(idx for idx, _ in parsed),
        "changed_indices": _changed_indices(original_canonical, revised),
        "first_record_soc_gap_kwh": _round6(start_gap),
        "revised_audit": revised_audit,
        "stored_at": stored_at,
    }


def get_corrections(session_id: str, store: Optional[SessionStore] = None) -> dict:
    """查询更正台账：原始记录、当前修订记录、差异下标与结算接口入参。

    原始记录始终为最初上报的一份（更正不改写）；settlement_request 携带
    当前修订记录，可直接 POST 给 /dispatch/settlement。
    """
    store = store or get_store()
    session, original_records, rows = _load_complete_session(store, session_id)
    dispatch = session["dispatch"]
    tol = session["audit_tolerance_kwh"]
    num_periods = session["num_periods"]

    correction_rows = store.list_corrections(session_id)
    current = int(correction_rows[-1]["revision"]) if correction_rows else 0

    original_canonical = _canonical(original_records)
    if correction_rows:
        revised = json.loads(correction_rows[-1]["records_json"])
    else:
        revised = list(original_canonical)
    revised_models = [TelemetryRecord.model_validate(p) for p in revised]

    # 查询时对当前修订整段重算审计（口径同会话查询的 confirmed_audit）
    revised_audit = _audit_range(
        revised_models, 0, dispatch["charge_efficiency"], dispatch["discharge_efficiency"], tol
    )

    revisions = []
    for row in correction_rows:
        entries = json.loads(row["entries_json"])
        revisions.append(
            {
                "revision": int(row["revision"]),
                "corrected_indices": sorted(e["index"] for e in entries),
                "stored_at": row["stored_at"],
            }
        )

    return {
        "session_id": session_id,
        "created_at": session["created_at"],
        "updated_at": correction_rows[-1]["stored_at"] if correction_rows else None,
        "audit_tolerance_kwh": tol,
        "num_periods": num_periods,
        "current_revision": current,
        "original_records": [
            {"index": i, "record": original_records[i], "stored_at": rows[i]["stored_at"]}
            for i in range(num_periods)
        ],
        "revised_records": [
            {"index": i, "record": revised_models[i]} for i in range(num_periods)
        ],
        "changed_indices": _changed_indices(original_canonical, revised),
        "revised_audit": revised_audit,
        "revisions": revisions,
        "settlement_request": {
            "dispatch": dispatch,
            "plan_snapshot": session["plan_snapshot"],
            "settlement_tolerance_kwh": tol,
            "records": revised,
        },
    }
