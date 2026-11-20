"""归档 / 恢复在线 / 区间总量可推出性的端到端与单元测试。"""

from datetime import date, timedelta

import pytest
from sqlalchemy import inspect, text

from app import database
from app.solver import Report, evaluate_interval_totals, solve


def _rep(rid, s, e, total):
    return {
        "report_id": rid,
        "start_date": s,
        "end_date": e,
        "total": total,
    }


D0 = "2024-03-01"


def _d(offset: int) -> str:
    return (date(2024, 3, 1) + timedelta(days=offset)).isoformat()


# --------------------------------------------------------------------------- #
# 求解器：区间总量唯一推出判定
# --------------------------------------------------------------------------- #


def test_interval_totals_empty_remaining_not_derivable():
    results = evaluate_interval_totals([], [(date(2024, 3, 1), date(2024, 3, 3))])
    assert len(results) == 1
    assert results[0].finite is False
    assert results[0].unique is False
    assert results[0].value is None


def test_interval_totals_bounds_and_uniqueness():
    # 只剩一份 [0,2]=6 的报表：整段总量唯一，子区间 [0,1] 不唯一
    remaining = [Report("A", date(2024, 3, 1), date(2024, 3, 3), 6)]
    results = evaluate_interval_totals(
        remaining,
        [
            (date(2024, 3, 1), date(2024, 3, 3)),  # 整段
            (date(2024, 3, 1), date(2024, 3, 2)),  # 子段
            (date(2024, 3, 3), date(2024, 3, 3)),  # 单日
        ],
    )
    assert results[0].unique is True and results[0].value == 6
    assert results[1].unique is False and results[1].finite is True
    assert results[2].unique is False and results[2].finite is True


def test_interval_totals_jigsaw_difference_gives_uniqueness():
    # A1: [0,2]=6，A2: [0,1]=4 => 区间 [2,2] 总量 = 6-4 = 2 唯一
    remaining = [
        Report("A1", date(2024, 3, 1), date(2024, 3, 3), 6),
        Report("A2", date(2024, 3, 1), date(2024, 3, 2), 4),
    ]
    results = evaluate_interval_totals(
        remaining, [(date(2024, 3, 3), date(2024, 3, 3))]
    )
    assert results[0].unique is True and results[0].value == 2


def test_interval_totals_infeasible_remaining_never_unique():
    remaining = [
        Report("A", date(2024, 3, 1), date(2024, 3, 1), 5),
        Report("B", date(2024, 3, 1), date(2024, 3, 1), 6),
    ]
    results = evaluate_interval_totals(
        remaining, [(date(2024, 3, 1), date(2024, 3, 1))]
    )
    assert results[0].unique is False


def test_archived_total_value_matches_original_in_feasible_full_set():
    # 全集自洽时，在线报表若唯一推出归档区间总量，值必等于原件总量
    full = [
        Report("A1", date(2024, 3, 1), date(2024, 3, 3), 6),
        Report("A2", date(2024, 3, 1), date(2024, 3, 2), 4),
        Report("D", date(2024, 3, 3), date(2024, 3, 3), 2),
    ]
    assert solve(full).feasible
    results = evaluate_interval_totals(full[:2], [(full[2].start_date, full[2].end_date)])
    assert results[0].unique and results[0].value == full[2].total


# --------------------------------------------------------------------------- #
# API：归档成功路径
# --------------------------------------------------------------------------- #


def _seed_jigsaw(client):
    # A1: x0+x1+x2=6；A2: x0+x1=4；D: x2=2；三者自洽
    resp = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep("A1", _d(0), _d(2), 6),
                _rep("A2", _d(0), _d(1), 4),
                _rep("D", _d(2), _d(2), 2),
            ]
        },
    )
    assert resp.status_code == 200, resp.text


def test_archive_success_moves_offline_and_keeps_daily_derivable(client):
    _seed_jigsaw(client)
    # D 的总量 2 可由 A1-A2 唯一推出，允许归档
    resp = client.post("/reports/archive", json={"report_ids": ["D"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "accepted": True,
        "archived_ids": ["D"],
        "online_count": 2,
        "archived_count": 1,
    }

    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert rows["D"]["status"] == "archived"
    assert rows["A1"]["status"] == "online" and rows["A2"]["status"] == "online"
    # 状态过滤
    only_archived = client.get("/reports", params={"status": "archived"}).json()
    assert [r["report_id"] for r in only_archived] == ["D"]
    only_online = client.get("/reports", params={"status": "online"}).json()
    assert {r["report_id"] for r in only_online} == {"A1", "A2"}

    # 查日明细只用在线报表：x2=2 仍可由 A1/A2 唯一推出
    q = client.get("/daily", params={"start_date": _d(0), "end_date": _d(2)}).json()
    by_day = {row["day"]: row for row in q["days"]}
    assert by_day[_d(2)]["status"] == "determined"
    assert by_day[_d(2)]["visitors"] == 2


def test_archive_duplicate_interval_report(client):
    # 两份完全同区间同总量的报表，归档其中一份：剩余一份仍唯一推出其总量
    client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep("A", _d(0), _d(2), 6),
                _rep("B", _d(0), _d(2), 6),
            ]
        },
    )
    resp = client.post("/reports/archive", json={"report_ids": ["A"]})
    assert resp.status_code == 200
    # 归档后查数只用 B：多日总量无法拆分，三天全部待补
    q = client.get("/daily", params={"start_date": _d(0), "end_date": _d(2)}).json()
    assert q["determined_count"] == 0 and q["pending_count"] == 3


def test_batch_archive_all_or_nothing(client):
    # G 是其区间上唯一的报表，总量无法被其余在线报表推出；
    # E、F 互为同区间同总量影子，归档 E 后 F 仍唯一推出其总量。
    client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep("G", _d(0), _d(2), 6),
                _rep("E", _d(5), _d(5), 1),
                _rep("F", _d(5), _d(5), 1),
            ]
        },
    )
    # 混合批次：E 可推出、G 不可 => 整批拒绝，且失败不改变任何状态
    resp = client.post("/reports/archive", json={"report_ids": ["E", "G"]})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["accepted"] is False
    assert detail["non_derivable_report_ids"] == ["G"]
    rows = client.get("/reports").json()
    assert all(r["status"] == "online" for r in rows)

    # 去掉不可推出的 G 后，E 单独归档成功
    resp = client.post("/reports/archive", json={"report_ids": ["E"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_ids"] == ["E"]
    assert resp.json()["online_count"] == 2 and resp.json()["archived_count"] == 1
    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert rows["E"]["status"] == "archived"


def test_archive_last_remaining_report_rejected(client):
    client.post(
        "/reports/batch",
        json={"reports": [_rep("Solo", _d(0), _d(0), 7)]},
    )
    resp = client.post("/reports/archive", json={"report_ids": ["Solo"]})
    assert resp.status_code == 409
    assert resp.json()["detail"]["non_derivable_report_ids"] == ["Solo"]
    rows = client.get("/reports").json()
    assert rows[0]["status"] == "online"


def test_archive_validation_errors(client):
    _seed_jigsaw(client)
    # 空批次
    assert client.post("/reports/archive", json={"report_ids": []}).status_code == 422
    # 不存在的编号
    resp = client.post("/reports/archive", json={"report_ids": ["Nope"]})
    assert resp.status_code == 422
    # 批内重复
    resp = client.post("/reports/archive", json={"report_ids": ["D", "D"]})
    assert resp.status_code == 422
    # 已归档的不能重复归档
    assert client.post("/reports/archive", json={"report_ids": ["D"]}).status_code == 200
    resp = client.post("/reports/archive", json={"report_ids": ["D"]})
    assert resp.status_code == 422
    # 非法状态过滤值
    assert client.get("/reports", params={"status": "weird"}).status_code == 422


# --------------------------------------------------------------------------- #
# 补录与归档交错：同号更正转在线、失去依据的归档件恢复
# --------------------------------------------------------------------------- #


def test_correction_of_archived_report_returns_online_with_version_bump(client):
    _seed_jigsaw(client)
    assert client.post("/reports/archive", json={"report_ids": ["D"]}).status_code == 200

    # 同号更正（档期调整为 [1,2]、总量 2，与 A1/A2 仍自洽：x1=0,x2=2）
    resp = client.post(
        "/reports/batch",
        json={"reports": [_rep("D", _d(1), _d(2), 2)]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replaced_ids"] == ["D"]
    assert body["restored_ids"] == []
    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert rows["D"]["status"] == "online"
    assert rows["D"]["version"] == 2
    assert body["reports"][0]["status"] == "online"


def test_correction_breaking_basis_restores_archived_report(client):
    _seed_jigsaw(client)
    assert client.post("/reports/archive", json={"report_ids": ["D"]}).status_code == 200

    # 更正 A2：新约束只固定 x1=0，D 的 x2 失去唯一依据（x0+x2=6 可分摊）
    resp = client.post(
        "/reports/batch",
        json={"reports": [_rep("A2", _d(1), _d(1), 0)]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restored_ids"] == ["D"]
    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert rows["D"]["status"] == "online"
    assert rows["A2"]["status"] == "online" and rows["A2"]["version"] == 2
    # 三份全部在线
    assert body["online_count"] == 3 and body["archived_count"] == 0


def test_failed_correction_does_not_touch_archived_state(client):
    _seed_jigsaw(client)
    assert client.post("/reports/archive", json={"report_ids": ["D"]}).status_code == 200

    # 与在线 + 归档报表矛盾的更正（A1 总量改成 99，而 D 仍要求 x2=2、A2 要求 x0+x1=4）
    resp = client.post(
        "/reports/batch",
        json={"reports": [_rep("A1", _d(0), _d(2), 99)]},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    # 最小冲突组：A1' 与 A2+D 的总量矛盾；三者两两自洽，故整组必要
    assert set(detail["conflict_report_ids"]) == {"A1", "A2", "D"}
    # 失败请求不改变任何报表状态
    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert rows["D"]["status"] == "archived"
    assert rows["A1"]["total"] == 6 and rows["A1"]["version"] == 1
    assert rows["A2"]["status"] == "online"


def test_archived_reports_participate_in_feasibility_check(client):
    _seed_jigsaw(client)
    assert client.post("/reports/archive", json={"report_ids": ["D"]}).status_code == 200
    # 新报表与归档原件 D 矛盾（D: x2=2，E: x2=5）=> 整批退回
    resp = client.post(
        "/reports/batch",
        json={"reports": [_rep("E", _d(2), _d(2), 5)]},
    )
    assert resp.status_code == 409
    assert "D" in resp.json()["detail"]["conflict_report_ids"]
    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert "E" not in rows and rows["D"]["status"] == "archived"


def test_interleaved_archive_and_upload_counts(client):
    _seed_jigsaw(client)
    # 归档 D，再补一份独立新报表 E：总数 4（在线 3、归档 1）
    assert client.post("/reports/archive", json={"report_ids": ["D"]}).status_code == 200
    resp = client.post(
        "/reports/batch",
        json={"reports": [_rep("E", _d(5), _d(5), 1)]},
    )
    assert resp.status_code == 200
    assert resp.json()["stored_count"] == 4
    assert resp.json()["online_count"] == 3 and resp.json()["archived_count"] == 1


# --------------------------------------------------------------------------- #
# 120 份上限含归档件
# --------------------------------------------------------------------------- #


def test_capacity_120_includes_archived(client):
    base = date(2024, 1, 1)
    batch = [
        _rep(
            f"R{i:03d}",
            (base + timedelta(days=2 * i)).isoformat(),
            (base + timedelta(days=2 * i)).isoformat(),
            i,
        )
        for i in range(120)
    ]
    assert client.post("/reports/batch", json={"reports": batch}).status_code == 200

    # 每份单日报表配一份同区间同总量的“影子”才能归档；这里直接归档首个单日报表
    # 不可行（无剩余依据），先构造可归档对：用 R000 与新增同区间副本会超容量，
    # 故改为验证：归档不改变总数，120 份时新编号仍被容量护栏拒绝。
    # 先让 R000 可归档：补录同区间同总量替换不行（占同名），用相邻两份推导。
    # R000 位于 day0；补录 R120 会被拒，所以先验证拒绝路径。
    over = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep(
                    "R120",
                    (base + timedelta(days=400)).isoformat(),
                    (base + timedelta(days=400)).isoformat(),
                    1,
                )
            ]
        },
    )
    assert over.status_code == 422 and "120" in over.text

    # 用两份在线报表支撑一份归档：R000(day0)=0，新增 day1 单日报表与 [day0,day1] 合计
    # 受容量限制无法新增，改为同号改造 R001（day2 位置不相邻）——
    # 直接走最小可归档构造：替换 R001 为 [day0,day1] 总量 0（与 R000=0 自洽）
    repl = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep(
                    "R001",
                    base.isoformat(),
                    (base + timedelta(days=1)).isoformat(),
                    0,
                )
            ]
        },
    )
    assert repl.status_code == 200, repl.text
    # 现在 R000(day0)=0 可由 R001=[day0,day1]=0 唯一推出（两天都被压成 0）
    arch = client.post("/reports/archive", json={"report_ids": ["R000"]})
    assert arch.status_code == 200, arch.text
    assert arch.json()["archived_count"] == 1 and arch.json()["online_count"] == 119

    # 总数仍为 120，新编号依旧被拒（上限含归档件）
    over2 = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep(
                    "R120",
                    (base + timedelta(days=400)).isoformat(),
                    (base + timedelta(days=400)).isoformat(),
                    1,
                )
            ]
        },
    )
    assert over2.status_code == 422 and "120" in over2.text


# --------------------------------------------------------------------------- #
# 旧库迁移：v1.0 表结构无 status 列
# --------------------------------------------------------------------------- #


def test_init_db_migrates_legacy_table_without_status():
    engine = database.engine
    database.Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE reports ("
                "report_id VARCHAR(64) PRIMARY KEY, "
                "start_date VARCHAR(10) NOT NULL, "
                "end_date VARCHAR(10) NOT NULL, "
                "total INTEGER NOT NULL, "
                "version INTEGER NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO reports VALUES "
                "('Legacy', '2024-03-01', '2024-03-01', 5, 1)"
            )
        )
    database.init_db()
    columns = {col["name"] for col in inspect(engine).get_columns("reports")}
    assert "status" in columns
    with engine.begin() as conn:
        status = conn.execute(
            text("SELECT status FROM reports WHERE report_id='Legacy'")
        ).scalar()
    assert status == database.STATUS_ONLINE
    # 迁移后服务可正常查数
    from app.main import service

    with database.SessionLocal() as session:
        service._invalidate()
        sol = service.current_solution(session)
        assert sol.determined == {date(2024, 3, 1): 5}
        service._invalidate()
    # 复原 conftest 期望的空库结构
    database.Base.metadata.drop_all(engine)
    database.Base.metadata.create_all(engine)
