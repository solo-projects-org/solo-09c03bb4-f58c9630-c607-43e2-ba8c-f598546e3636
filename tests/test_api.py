"""端到端 API 测试（FastAPI TestClient + 临时 SQLite）。"""

from datetime import date

import pytest


def _rep(rid, s, e, total):
    return {
        "report_id": rid,
        "start_date": s,
        "end_date": e,
        "total": total,
    }


def test_health_and_empty_query(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/reports").json() == []
    resp = client.get("/daily")
    assert resp.status_code == 404


def test_batch_upload_then_query(client):
    payload = {
        "reports": [
            _rep("A", "2024-03-01", "2024-03-03", 5),
            _rep("B", "2024-03-02", "2024-03-02", 2),
        ]
    }
    resp = client.post("/reports/batch", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] is True
    assert body["stored_count"] == 2
    assert body["inserted_ids"] == ["A", "B"]

    q = client.get("/daily", params={"start_date": "2024-03-01", "end_date": "2024-03-03"}).json()
    by_day = {row["day"]: row for row in q["days"]}
    assert by_day["2024-03-01"]["status"] == "pending"
    assert by_day["2024-03-02"] == {
        "day": "2024-03-02",
        "status": "determined",
        "visitors": 2,
        "covered": True,
    }
    assert by_day["2024-03-03"]["status"] == "pending"
    assert q["determined_count"] == 1 and q["pending_count"] == 2

    # 区间外日期：未覆盖，待补
    q2 = client.get("/daily", params={"start_date": "2024-02-28", "end_date": "2024-02-28"}).json()
    assert q2["days"][0]["covered"] is False and q2["days"][0]["visitors"] is None


def test_conflicting_batch_rejected_with_minimal_group(client):
    client.post(
        "/reports/batch",
        json={"reports": [_rep("A", "2024-03-01", "2024-03-03", 5)]},
    )
    # 与 A 同区间、不同总量 => 矛盾；再带一份独立报表 C
    resp = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep("B", "2024-03-01", "2024-03-03", 9),
                _rep("C", "2024-03-10", "2024-03-10", 1),
            ]
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert set(detail["conflict_report_ids"]) == {"A", "B"}
    # 整批退回：C 也不得入库
    ids = {row["report_id"] for row in client.get("/reports").json()}
    assert ids == {"A"}


def test_replacement_is_whole_report_and_revises_daily_values(client):
    client.post(
        "/reports/batch",
        json={"reports": [_rep("A", "2024-03-01", "2024-03-01", 5)]},
    )
    # 整份替换为跨 3 天、总量 0 => 三天都唯一为 0
    resp = client.post(
        "/reports/batch",
        json={"reports": [_rep("A", "2024-03-01", "2024-03-03", 0)]},
    )
    assert resp.status_code == 200
    assert resp.json()["replaced_ids"] == ["A"]
    rows = client.get("/reports").json()
    assert len(rows) == 1 and rows[0]["version"] == 2
    q = client.get("/daily", params={"start_date": "2024-03-01", "end_date": "2024-03-03"}).json()
    assert all(row["visitors"] == 0 for row in q["days"])

    # 再来一份使 0 的唯一依据失效（总量改为 3）：全部转回待补
    client.post(
        "/reports/batch",
        json={"reports": [_rep("A", "2024-03-01", "2024-03-03", 3)]},
    )
    q = client.get("/daily", params={"start_date": "2024-03-01", "end_date": "2024-03-03"}).json()
    assert q["determined_count"] == 0 and q["pending_count"] == 3


def test_validation_errors(client):
    # 档期 91 天
    bad = _rep("X", "2024-03-01", "2024-05-30", 1)
    assert client.post("/reports/batch", json={"reports": [bad]}).status_code == 422
    # 负数
    bad2 = _rep("X", "2024-03-01", "2024-03-01", -1)
    assert client.post("/reports/batch", json={"reports": [bad2]}).status_code == 422
    # 空批次
    assert client.post("/reports/batch", json={"reports": []}).status_code == 422
    # 批内重复编号
    dup = {
        "reports": [
            _rep("X", "2024-03-01", "2024-03-01", 1),
            _rep("X", "2024-03-02", "2024-03-02", 1),
        ]
    }
    assert client.post("/reports/batch", json=dup).status_code == 422


def test_capacity_limit_120(client):
    # 先放 119 份互不重叠的单日报表（同区间不同总量会互相冲突）
    from datetime import date, timedelta

    base = date(2024, 1, 1)
    batch = [
        _rep(
            f"R{i:03d}",
            (base + timedelta(days=i)).isoformat(),
            (base + timedelta(days=i)).isoformat(),
            i,
        )
        for i in range(119)
    ]
    assert client.post("/reports/batch", json={"reports": batch}).status_code == 200
    # 再补 2 份新编号 -> 121，拒绝
    over = [
        _rep(
            "R119",
            (base + timedelta(days=119)).isoformat(),
            (base + timedelta(days=119)).isoformat(),
            1,
        ),
        _rep(
            "R120",
            (base + timedelta(days=120)).isoformat(),
            (base + timedelta(days=120)).isoformat(),
            1,
        ),
    ]
    resp = client.post("/reports/batch", json={"reports": over})
    assert resp.status_code == 422 and "120" in resp.text
    # 替换已有编号不占名额，仍然成功
    ok = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep(
                    "R000",
                    base.isoformat(),
                    base.isoformat(),
                    99,
                )
            ]
        },
    )
    assert ok.status_code == 200 and ok.json()["stored_count"] == 119


def test_transactional_rollback_on_conflict(client):
    client.post(
        "/reports/batch",
        json={"reports": [_rep("A", "2024-03-01", "2024-03-01", 5)]},
    )
    # 同批里既有可替换的 A，又有与现行报表矛盾的 B（B 与 A 同区间不同总量）
    resp = client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep("A", "2024-03-01", "2024-03-01", 6),
                _rep("B", "2024-03-01", "2024-03-01", 7),
            ]
        },
    )
    assert resp.status_code == 409
    # 整批退回：A 保持原值、版本不变，B 未入库
    rows = {r["report_id"]: r for r in client.get("/reports").json()}
    assert set(rows) == {"A"}
    assert rows["A"]["total"] == 5 and rows["A"]["version"] == 1


def test_default_query_range_covers_all_reports(client):
    client.post(
        "/reports/batch",
        json={
            "reports": [
                _rep("A", "2024-03-01", "2024-03-01", 3),
                _rep("B", "2024-03-05", "2024-03-05", 4),
            ]
        },
    )
    q = client.get("/daily").json()
    assert q["start_date"] == "2024-03-01" and q["end_date"] == "2024-03-05"
    assert len(q["days"]) == 5
    determined = {row["day"]: row["visitors"] for row in q["days"] if row["status"] == "determined"}
    assert determined == {"2024-03-01": 3, "2024-03-05": 4}
