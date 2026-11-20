"""solver 单元测试。"""

from datetime import date, timedelta

import pytest

from app.solver import (
    MAX_REPORT_COUNT,
    MAX_SPAN_DAYS,
    Report,
    ReportValidationError,
    find_minimal_conflict,
    solve,
)

D = date(2024, 3, 1)


def d(offset: int) -> date:
    return D + timedelta(days=offset)


def r(rid: str, s: int, e: int, total: int) -> Report:
    return Report(rid, d(s), d(e), total)


# --------------------------------------------------------------------------- #
# 单份校验
# --------------------------------------------------------------------------- #


def test_reject_span_over_90_days():
    with pytest.raises(ReportValidationError):
        r("X", 0, MAX_SPAN_DAYS, 10).validate()  # 91 天


def test_accept_exactly_90_days():
    r("X", 0, MAX_SPAN_DAYS - 1, 10).validate()  # 90 天，不报错


@pytest.mark.parametrize("bad_total", [-1, 1.5, True])
def test_reject_bad_total(bad_total):
    with pytest.raises(ReportValidationError):
        Report("X", d(0), d(1), bad_total).validate()


def test_reject_reversed_dates_and_blank_id():
    with pytest.raises(ReportValidationError):
        r("X", 3, 1, 10).validate()
    with pytest.raises(ReportValidationError):
        Report(" ", d(0), d(1), 10).validate()


# --------------------------------------------------------------------------- #
# 可行性与唯一确定
# --------------------------------------------------------------------------- #


def test_single_one_day_report_is_determined():
    sol = solve([r("A", 0, 0, 7)])
    assert sol.feasible
    assert sol.determined == {d(0): 7}


def test_single_multi_day_report_only_pending():
    sol = solve([r("A", 0, 2, 6)])  # 三天总和 6，每天可变
    assert sol.feasible
    assert sol.determined == {}
    assert sol.covered_undetermined == frozenset({d(0), d(1), d(2)})


def test_zero_total_forces_all_zero():
    sol = solve([r("A", 0, 2, 0)])
    assert sol.determined == {d(0): 0, d(1): 0, d(2): 0}


def test_overlapping_reports_partial_determination():
    sol = solve([r("A", 0, 2, 5), r("B", 1, 1, 2)])
    # x1=2 唯一；x0+x2=3，两者各自待补
    assert sol.determined == {d(1): 2}
    assert set(sol.covered_undetermined) == {d(0), d(2)}


def test_jigsaw_reports_determine_every_day():
    sol = solve(
        [
            r("A", 0, 3, 10),  # x0..x3 = 10
            r("B", 0, 1, 4),  # x0+x1 = 4
            r("C", 2, 3, 6),  # x2+x3 = 6
            r("D", 1, 1, 1),  # x1 = 1
            r("E", 2, 2, 6),  # x2 = 6
        ]
    )
    assert sol.feasible
    assert sol.determined == {d(0): 3, d(1): 1, d(2): 6, d(3): 0}


def test_conflicting_same_interval_different_totals():
    sol = solve([r("A", 0, 2, 5), r("B", 0, 2, 6)])
    assert not sol.feasible


def test_minimal_conflict_group_removal_property():
    reports = [
        r("A", 0, 2, 5),
        r("B", 0, 2, 6),  # 与 A 直接矛盾
        r("C", 3, 4, 2),  # 独立、自洽，不应入组
    ]
    group = find_minimal_conflict(reports)
    assert set(group) == {"A", "B"}
    by_id = {x.report_id: x for x in reports}
    # 去掉组内任一份，其余即可自洽
    for member in group:
        rest = [rep for rid, rep in by_id.items() if rid != member]
        assert solve(rest).feasible


def test_three_way_conflict_each_pair_consistent():
    # 三份报表两两自洽、三者同时成立矛盾：
    # A: x0+x1 = 1, B: x1+x2 = 1, C: x0+x1+x2 = 3
    # 两两：A+B 可行（x1=1）；A+C => x2=2 可行；B+C => x0=2 可行；
    # 三者：由 A、B 得 x0+x2+2x1=2，结合 C => x1 = -1，不可行。
    reports = [r("A", 0, 1, 1), r("B", 1, 2, 1), r("C", 0, 2, 3)]
    assert not solve(reports).feasible
    assert solve(reports[:2]).feasible
    assert solve([reports[0], reports[2]]).feasible
    assert solve([reports[1], reports[2]]).feasible
    group = find_minimal_conflict(reports)
    assert set(group) == {"A", "B", "C"}
    by_id = {x.report_id: x for x in reports}
    for member in group:
        rest = [rep for rid, rep in by_id.items() if rid != member]
        assert solve(rest).feasible


def test_same_id_replaced_not_duplicated():
    sol = solve([r("A", 0, 0, 5), r("A", 0, 0, 9)])
    assert sol.feasible
    assert sol.determined == {d(0): 9}
    assert len(sol.reports) == 1


def test_adjacent_reports_chain():
    sol = solve([r("A", 0, 0, 3), r("B", 1, 1, 8), r("C", 0, 1, 11)])
    assert sol.determined == {d(0): 3, d(1): 8}


def test_empty_system():
    sol = solve([])
    assert sol.feasible and sol.determined == {}
