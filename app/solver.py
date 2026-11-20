"""客流报表约束求解核心（纯函数，不依赖数据库 / Web 框架）。

建模
----
每份报表 r = (编号, 首天 s, 末日 e, 总人次 t) 表示

    sum_{d in [s, e]} x_d = t,   x_d >= 0 且为整数

引入前缀和 P_d = sum_{k <= d} x_k（锚点 P_0 = 0），报表约束变为

    P_e - P_{s-1} = t
    P_d - P_{d-1} >= 0                 （日人次非负）

这是差分约束系统。对 P_v - P_u >= w 建图 u -> v，边权 w：

* 报表等式拆成两条边：(s-1) -> e，权 t；e -> (s-1)，权 -t
* 相邻节点间补权 0 的链边（非负性）

为控制规模，图中只放报表端点（s-1、e），节点数 <= 241。

可行性
~~~~~~
系统可行 <=> 图中没有正权环。Floyd-Warshall 求全源最长路 L，
若存在 L[i][i] > 0 即不可行。

逐日唯一值
~~~~~~~~~~
设相邻两个图节点 a < b 之间（空档 (a, b]）没有任何报表端点，则该空档内
每天的人次只受“空档总量 P_b - P_a 非负、可在各日间任意拆分”约束：

* 空档长度为 1 天：该日人次就是 P_b - P_a，
  最小值 L[a][b]，最大值 -L[b][a]（最短路 = 反向最长路取负）；
* 空档长度 >= 2 天：任一单日的最小值为 0（流量可放在别日），
  最大值仍为 -L[b][a]（单日可吸走整个空档的流量）。

最小值 = 最大值（且有限）时，该日人次被唯一确定；整数边权保证结果为
整数。日期不在任何报表区间内时没有有限界，返回“待补”。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Sequence

import numpy as np

MAX_SPAN_DAYS = 90
MAX_REPORT_COUNT = 120
# 现实护栏：90 天景区总人次不可能超过 10^12，同时保证 int64 路径和不溢出。
MAX_TOTAL = 10**12
_NEG = -(10**15)


class ReportValidationError(ValueError):
    """单份报表自身不合法（跨期超 90 天、负数等）。"""


@dataclass(frozen=True)
class Report:
    report_id: str
    start_date: date
    end_date: date
    total: int

    def validate(self) -> None:
        if not self.report_id or not self.report_id.strip():
            raise ReportValidationError("报表编号不能为空")
        if self.end_date < self.start_date:
            raise ReportValidationError(
                f"报表 {self.report_id} 的末日 {self.end_date} 早于首天 {self.start_date}"
            )
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_SPAN_DAYS:
            raise ReportValidationError(
                f"报表 {self.report_id} 档期 {span} 天，超过最长 {MAX_SPAN_DAYS} 天"
            )
        if not isinstance(self.total, int) or isinstance(self.total, bool):
            raise ReportValidationError(f"报表 {self.report_id} 的总人次必须是整数")
        if self.total < 0:
            raise ReportValidationError(f"报表 {self.report_id} 的总人次不能为负")
        if self.total > MAX_TOTAL:
            raise ReportValidationError(
                f"报表 {self.report_id} 的总人次 {self.total} 超过上限 {MAX_TOTAL}"
            )


@dataclass
class SolvedSystem:
    """一套生效报表的求解结果。"""

    feasible: bool
    reports: tuple[Report, ...]
    # 覆盖日期 -> 唯一次数；未出现的日期即未覆盖（待补）
    determined: dict[date, int]
    # 覆盖但无法唯一确定的日期
    covered_undetermined: frozenset[date]


def _floyd_longest(node_count: int, edges: Sequence[tuple[int, int, int]]) -> np.ndarray:
    """全源最长路。不可达为 _NEG；正权环表现为对角线 > 0。"""
    mat = np.full((node_count, node_count), _NEG, dtype=np.int64)
    np.fill_diagonal(mat, 0)
    for u, v, w in edges:
        if w > mat[u, v]:
            mat[u, v] = w
    for k in range(node_count):
        mat = np.maximum(mat, mat[:, k, None] + mat[None, k, :])
    return mat


def solve(reports: Iterable[Report]) -> SolvedSystem:
    """对一整套报表求解可行性与每日唯一值。"""
    unique: dict[str, Report] = {}
    for r in reports:
        r.validate()
        unique[r.report_id] = r  # 同编号以后者为准（整份替换语义）
    ordered = tuple(unique.values())

    if not ordered:
        return SolvedSystem(True, (), {}, frozenset())

    # --- 压缩节点：锚点 0 为 P_0，其余节点按日期排序 ---------------------
    boundary_set: set[date] = set()
    covered: set[date] = set()
    for r in ordered:
        boundary_set.add(r.start_date - timedelta(days=1))
        boundary_set.add(r.end_date)
        cur = r.start_date
        while cur <= r.end_date:
            covered.add(cur)
            cur += timedelta(days=1)

    node_dates = sorted(boundary_set)
    # 矩阵索引：0 = 锚点；list 中第 p 个日期 -> 索引 p+1
    idx = {d: p + 1 for p, d in enumerate(node_dates)}
    anchor_date = node_dates[0] - timedelta(days=1)
    node_date_with_anchor = [anchor_date] + node_dates
    n = len(node_dates) + 1

    edges: list[tuple[int, int, int]] = []
    # 非负链边（含锚点到最早节点）
    for i in range(1, n):
        edges.append((i - 1, i, 0))
    # 报表等式边
    for r in ordered:
        s_node = idx[r.start_date - timedelta(days=1)]
        e_node = idx[r.end_date]
        edges.append((s_node, e_node, r.total))
        edges.append((e_node, s_node, -r.total))

    longest = _floyd_longest(n, edges)
    if np.any(np.diag(longest) > 0):
        return SolvedSystem(False, ordered, {}, frozenset(covered))

    # --- 逐日上下界 ------------------------------------------------------
    # node_dates[i] 的矩阵索引为 i+1；锚点（索引 0）对应日期为其前一天。
    determined: dict[date, int] = {}
    undetermined: set[date] = set()
    for d in covered:
        p = bisect_left(node_dates, d)  # 第一个 >= d 的节点日期（0 基位置）
        if p < len(node_dates) and node_dates[p] == d:
            lo = p          # 左侧节点矩阵索引 = p+1-1 = p
            hi = p + 1      # d 本身是节点
        else:
            lo = p          # 左侧节点 = node_dates[p-1]，矩阵索引 p
            hi = p + 1      # 右侧节点 = node_dates[p]，矩阵索引 p+1
        gap_days = (node_date_with_anchor[hi] - node_date_with_anchor[lo]).days
        x_min = int(longest[lo, hi]) if gap_days == 1 else 0
        x_max = int(-longest[hi, lo])
        if x_min == x_max:
            determined[d] = x_min
        else:
            undetermined.add(d)

    return SolvedSystem(
        feasible=True,
        reports=ordered,
        determined=determined,
        covered_undetermined=frozenset(undetermined),
    )


def find_minimal_conflict(
    reports: Sequence[Report],
    protected_ids: set[str] | None = None,
) -> list[str]:
    """在不可行的报表集合中求一个最小冲突组（删除型 IIS）。

    最小性：去掉组内任意一份，其余报表即可同时成立。
    protected_ids 中的报表尽量留在组外（优先拿已生效报表做删除尝试），
    但若冲突本身要求它在组内，仍会被纳入。
    """
    protected = protected_ids or set()
    pool = list(reports)
    if solve(pool).feasible:
        return []

    # 删除法：按“受保护者靠后尝试删除”排序，逐个检查删掉后是否仍不可行；
    # 仍不可行说明它不是必要成员，直接移除。剩下的集合不可约 => 最小冲突组。
    ordered = sorted(pool, key=lambda x: (x.report_id in protected, x.report_id))
    kept = list(ordered)
    for r in ordered:
        rest = [x for x in kept if x is not r]
        if not solve(rest).feasible:
            kept = rest
    return sorted(r.report_id for r in kept)
