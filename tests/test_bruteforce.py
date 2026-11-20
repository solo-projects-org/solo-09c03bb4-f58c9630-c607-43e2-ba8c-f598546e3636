"""随机暴力校验：穷举所有非负整数日向量，核对求解器的可行性与逐日界。"""

import random
from datetime import date, timedelta
from itertools import product

from app.solver import Report, solve

D0 = date(2024, 1, 1)


def _brute_force(reports, days_n, cap=8):
    """枚举所有分量 0..cap 的日向量，返回 (是否可行, 每日最小值, 每日最大值)。"""
    mins = [None] * days_n
    maxs = [None] * days_n
    feasible = False
    for vec in product(range(cap + 1), repeat=days_n):
        ok = True
        for rep in reports:
            s = (rep.start_date - D0).days
            e = (rep.end_date - D0).days
            if sum(vec[s : e + 1]) != rep.total:
                ok = False
                break
        if not ok:
            continue
        feasible = True
        for i, v in enumerate(vec):
            mins[i] = v if mins[i] is None else min(mins[i], v)
            maxs[i] = v if maxs[i] is None else max(maxs[i], v)
    return feasible, mins, maxs


def _random_reports(rng, days_n):
    count = rng.randint(1, 5)
    reports = []
    for k in range(count):
        s = rng.randint(0, days_n - 1)
        e = rng.randint(s, days_n - 1)
        # 小总量保证可行枚举空间不大；含 0 与相等总量等边界
        total = rng.randint(0, 6)
        reports.append(
            Report(f"R{k}", D0 + timedelta(days=s), D0 + timedelta(days=e), total)
        )
    return reports


def test_solver_matches_brute_force():
    rng = random.Random(20240301)
    trials = 60
    for _ in range(trials):
        days_n = rng.randint(2, 4)
        reports = _random_reports(rng, days_n)
        sol = solve(reports)
        bf_feasible, bf_min, bf_max = _brute_force(reports, days_n, cap=8)
        assert sol.feasible == bf_feasible, reports
        if not bf_feasible:
            continue
        for i in range(days_n):
            day = D0 + timedelta(days=i)
            if bf_min[i] is None:
                continue  # 暴力枚举没覆盖到可行解（cap 太小），跳过该日
            covered_by_solver = day in sol.determined or day in sol.covered_undetermined
            if not covered_by_solver:
                continue  # 该日不在任何报表区间内，求解器不负责
            if bf_min[i] == bf_max[i]:
                assert sol.determined.get(day) == bf_min[i], (reports, day)
            else:
                assert day not in sol.determined, (reports, day)
                assert day in sol.covered_undetermined


def test_known_case_interior_gap_values():
    # 空档长度 2：总量 4 由两日分摊，任何单日都不唯一
    rep = Report("A", D0, D0 + timedelta(days=1), 4)
    sol = solve([rep])
    assert sol.feasible and not sol.determined
    # 与暴力结果对照：单日 min=0, max=4
    bf = _brute_force([rep], 2, cap=8)
    assert bf[0] and bf[1] == [0, 0] and bf[2] == [4, 4]


def test_known_case_split_determines_everything():
    reports = [
        Report("A", D0, D0 + timedelta(days=2), 5),
        Report("B", D0 + timedelta(days=1), D0 + timedelta(days=2), 2),
    ]
    sol = solve(reports)
    # x0 = 3 唯一，x1+x2=2 各自待补
    assert sol.determined == {D0: 3}
    bf = _brute_force(reports, 3, cap=8)
    assert bf[0]
    assert bf[1] == [3, 0, 0]
    assert bf[2] == [3, 2, 2]
