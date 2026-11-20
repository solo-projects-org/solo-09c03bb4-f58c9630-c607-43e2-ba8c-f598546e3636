"""随机化属性测试与规模压测。"""

import random
from datetime import date, timedelta

from app.solver import MAX_REPORT_COUNT, Report, find_minimal_conflict, solve

D0 = date(2024, 1, 1)


def _random_reports(rng, n, days_n=20):
    out = []
    for k in range(n):
        s = rng.randint(0, days_n - 1)
        e = rng.randint(s, days_n - 1)
        out.append(
            Report(
                f"R{k:03d}",
                D0 + timedelta(days=s),
                D0 + timedelta(days=e),
                rng.randint(0, 30),
            )
        )
    return out


def test_minimal_conflict_removal_property_random():
    """每个随机不可行实例：冲突组去掉任一成员后，组内其余报表必须可行。"""
    rng = random.Random(77)
    checked = 0
    for _ in range(200):
        reports = _random_reports(rng, rng.randint(2, 8))
        if solve(reports).feasible:
            continue
        group = find_minimal_conflict(reports)
        assert group, "不可行实例必须返回非空冲突组"
        by_id = {r.report_id: r for r in reports}
        group_reports = [by_id[g] for g in group]
        assert not solve(group_reports).feasible
        for member in group:
            rest = [rep for rep in group_reports if rep.report_id != member]
            assert solve(rest).feasible, (group, member)
        checked += 1
    assert checked >= 30, f"随机到的不可行实例过少: {checked}"


def test_full_scale_120_reports_performance():
    """120 份、每份最长 90 天的极端规模下，求解与冲突诊断仍快速完成。"""
    import time

    rng = random.Random(99)
    reports = []
    base = date(2024, 1, 1)
    for k in range(MAX_REPORT_COUNT):
        s = base + timedelta(days=rng.randint(0, 300))
        e = s + timedelta(days=rng.randint(0, 89))  # 档期 <= 90 天
        reports.append(Report(f"R{k:03d}", s, e, rng.randint(0, 10000)))

    t0 = time.perf_counter()
    sol = solve(reports)
    solve_ms = (time.perf_counter() - t0) * 1000
    assert solve_ms < 2000, f"单次求解过慢: {solve_ms:.0f}ms"

    if not sol.feasible:
        t0 = time.perf_counter()
        group = find_minimal_conflict(reports)
        iis_ms = (time.perf_counter() - t0) * 1000
        assert iis_ms < 60000, f"冲突诊断过慢: {iis_ms:.0f}ms"
        group_reports = [r for r in reports if r.report_id in group]
        assert not solve(group_reports).feasible
        for member in group:
            rest = [r for r in group_reports if r.report_id != member]
            assert solve(rest).feasible


def test_full_scale_feasible_case_determinacy():
    """构造 120 份自洽报表：底层随机生成日人次，报表取区间和，逐日结果必须吻合。"""
    rng = random.Random(123)
    days_n = 360
    truth = [rng.randint(0, 50) for _ in range(days_n)]
    prefix = [0]
    for v in truth:
        prefix.append(prefix[-1] + v)

    reports = []
    for k in range(MAX_REPORT_COUNT):
        s = rng.randint(0, days_n - 1)
        e = min(days_n - 1, s + rng.randint(0, 89))  # 档期 <= 90 天
        total = prefix[e + 1] - prefix[s]
        reports.append(
            Report(f"R{k:03d}", D0 + timedelta(days=s), D0 + timedelta(days=e), total)
        )
    sol = solve(reports)
    assert sol.feasible
    # 被唯一确定的日期必须等于底层真值
    for day, value in sol.determined.items():
        i = (day - D0).days
        assert value == truth[i]
