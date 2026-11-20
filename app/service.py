"""业务服务层：整批原子提交、归档判定、冲突诊断、求解缓存。

报表分两种状态：

* 在线（online）：参与在线求解与日明细查数；
* 归档（archived）：原件留档备查，不参与查数，但其档期总量必须始终能由
  当前在线报表唯一推出——每次写操作后都会重新核对，失去依据的归档件随
  补报恢复在线。

所有写操作经同一把锁串行化，且全部先在提交时的数据快照上完成验算，
验算失败不写库（单事务回滚），报表状态不变。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import solver
from .database import STATUS_ARCHIVED, STATUS_ONLINE, ReportRow
from .solver import MAX_REPORT_COUNT, Report, ReportValidationError, SolvedSystem


class CapacityExceededError(Exception):
    """报表数量超过 120 份上限（含归档件）。"""


class BatchConflictError(Exception):
    """整批与已生效报表无法同时成立。"""

    def __init__(self, conflict_ids: list[str], reason: str):
        self.conflict_ids = conflict_ids
        self.reason = reason
        super().__init__(reason)


class ArchiveConditionError(Exception):
    """整批移出后，部分报表总量无法由剩余在线报表唯一推出。"""

    def __init__(self, non_derivable_ids: list[str], reason: str):
        self.non_derivable_ids = non_derivable_ids
        self.reason = reason
        super().__init__(reason)


class ReportService:
    """所有写操作经同一把锁串行化；读操作走快照 + 版本缓存。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: SolvedSystem | None = None  # 在线报表求解结果，写操作后失效

    # ------------------------------------------------------------------ #
    # 映射
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_report(row: ReportRow) -> Report:
        return Report(
            report_id=row.report_id,
            start_date=date.fromisoformat(row.start_date),
            end_date=date.fromisoformat(row.end_date),
            total=row.total,
        )

    def _load_rows(self, session: Session, status: str | None = None) -> list[ReportRow]:
        stmt = select(ReportRow).order_by(ReportRow.report_id)
        if status is not None:
            stmt = stmt.where(ReportRow.status == status)
        return list(session.scalars(stmt).all())

    def _load_online(self, session: Session) -> list[Report]:
        return [self._row_to_report(r) for r in self._load_rows(session, STATUS_ONLINE)]

    # ------------------------------------------------------------------ #
    # 求解缓存（只依赖当前在线报表内容版本）
    # ------------------------------------------------------------------ #
    def current_solution(self, session: Session) -> SolvedSystem:
        with self._lock:
            if self._cache is None:
                self._cache = solver.solve(self._load_online(session))
            return self._cache

    def _invalidate(self) -> None:
        self._cache = None

    # ------------------------------------------------------------------ #
    # 整批补录
    # ------------------------------------------------------------------ #
    def upload_batch(self, session: Session, incoming: list[Report]) -> dict:
        with self._lock:
            # 1) 单份合法性（编号、90 天上限、非负整数）。
            for r in incoming:
                r.validate()
            ids_in_batch = [r.report_id for r in incoming]
            if len(set(ids_in_batch)) != len(ids_in_batch):
                dupes = sorted({x for x in ids_in_batch if ids_in_batch.count(x) > 1})
                raise ReportValidationError(f"同一批内编号重复: {', '.join(dupes)}")

            existing_rows = {
                row.report_id: row
                for row in session.scalars(select(ReportRow)).all()
            }
            replaced_ids = [rid for rid in ids_in_batch if rid in existing_rows]
            inserted_ids = [rid for rid in ids_in_batch if rid not in existing_rows]

            # 2) 120 份容量护栏（在线 + 归档合计；替换不占新名额）。
            projected_count = len(existing_rows) + len(inserted_ids)
            if projected_count > MAX_REPORT_COUNT:
                raise CapacityExceededError(
                    f"入库后将有 {projected_count} 份报表（含归档件），"
                    f"超过最多 {MAX_REPORT_COUNT} 份上限"
                )

            # 3) 补录与在线及归档报表一起验算：用“替换后的整份视图”拼出候选集合。
            merged: dict[str, Report] = {
                rid: self._row_to_report(row) for rid, row in existing_rows.items()
            }
            for r in incoming:
                merged[r.report_id] = r  # 同编号整份替换
            candidate = list(merged.values())

            candidate_solution = solver.solve(candidate)
            if not candidate_solution.feasible:
                # 4) 求最小冲突组：优先把已生效报表纳入删除尝试，
                #    尽量让诊断组聚焦在导致矛盾的必要成员上。
                conflict_ids = solver.find_minimal_conflict(
                    candidate, protected_ids=set(inserted_ids)
                )
                raise BatchConflictError(
                    conflict_ids,
                    "本批报表与在线及归档报表无法同时成立，整批退回；"
                    "去掉冲突组内任意一份后其余报表可自洽",
                )

            # 5) 同号更正后的报表一律转为在线；其余未被本批触及的归档件，
            #    按补录后的在线报表重新核对“总量可由在线报表唯一推出”，
            #    失去依据者随补报恢复在线，其余继续归档。
            incoming_ids = set(ids_in_batch)
            post_online_reports: list[Report] = []
            archived_to_check: list[ReportRow] = []
            for rid, row in existing_rows.items():
                if rid in incoming_ids:
                    continue  # 被整份替换，落库时转在线
                if row.status == STATUS_ARCHIVED:
                    archived_to_check.append(row)
                else:
                    post_online_reports.append(self._row_to_report(row))
            post_online_reports.extend(incoming)

            restored_rows: list[ReportRow] = []
            if archived_to_check:
                results = solver.evaluate_interval_totals(
                    post_online_reports,
                    [
                        (
                            date.fromisoformat(row.start_date),
                            date.fromisoformat(row.end_date),
                        )
                        for row in archived_to_check
                    ],
                )
                for row, res in zip(archived_to_check, results, strict=True):
                    if not res.unique:
                        restored_rows.append(row)

            # 6) 全部通过 -> 原子落库（同事务，异常整体回滚）。
            try:
                for r in incoming:
                    row = existing_rows.get(r.report_id)
                    if row is None:
                        session.add(
                            ReportRow(
                                report_id=r.report_id,
                                start_date=r.start_date.isoformat(),
                                end_date=r.end_date.isoformat(),
                                total=r.total,
                                version=1,
                                status=STATUS_ONLINE,
                            )
                        )
                    else:
                        row.start_date = r.start_date.isoformat()
                        row.end_date = r.end_date.isoformat()
                        row.total = r.total
                        row.version += 1
                        row.status = STATUS_ONLINE  # 同号更正 -> 转在线
                for row in restored_rows:
                    row.status = STATUS_ONLINE
                session.commit()
            except Exception:
                session.rollback()
                raise
            self._invalidate()

            final_rows = session.scalars(
                select(ReportRow).where(ReportRow.report_id.in_(ids_in_batch))
            ).all()
            online_count = session.scalar(
                select(func.count())
                .select_from(ReportRow)
                .where(ReportRow.status == STATUS_ONLINE)
            )
            archived_count = session.scalar(
                select(func.count())
                .select_from(ReportRow)
                .where(ReportRow.status == STATUS_ARCHIVED)
            )
            return {
                "stored_count": online_count + archived_count,
                "online_count": online_count,
                "archived_count": archived_count,
                "replaced_ids": sorted(replaced_ids),
                "inserted_ids": sorted(inserted_ids),
                "restored_ids": sorted(row.report_id for row in restored_rows),
                "rows": final_rows,
            }

    # ------------------------------------------------------------------ #
    # 整批归档
    # ------------------------------------------------------------------ #
    def archive_batch(self, session: Session, report_ids: list[str]) -> dict:
        with self._lock:
            # 1) 入参与当前状态核对（按提交时的数据判断）。
            ids = list(dict.fromkeys(report_ids))  # 去重保序
            if len(ids) != len(report_ids):
                dupes = sorted({x for x in report_ids if report_ids.count(x) > 1})
                raise ReportValidationError(f"同一批内归档编号重复: {', '.join(dupes)}")

            rows_by_id = {
                row.report_id: row
                for row in session.scalars(select(ReportRow)).all()
            }
            missing = [rid for rid in ids if rid not in rows_by_id]
            if missing:
                raise ReportValidationError(
                    f"以下报表编号不存在，无法归档: {', '.join(sorted(missing))}"
                )
            not_online = sorted(
                rid for rid in ids if rows_by_id[rid].status != STATUS_ONLINE
            )
            if not_online:
                raise ReportValidationError(
                    f"以下报表不是在线状态（已归档或不存在），无法重复归档: "
                    f"{', '.join(not_online)}"
                )

            target_set = set(ids)
            online_rows = self._load_rows(session, STATUS_ONLINE)
            remaining_rows = [row for row in online_rows if row.report_id not in target_set]
            target_rows = [rows_by_id[rid] for rid in ids]

            # 2) 只有“移出整批后”剩余在线报表仍能唯一推出每份原件的区间
            #    总人次，才允许归档；一次建图批量判定。
            results = solver.evaluate_interval_totals(
                [self._row_to_report(row) for row in remaining_rows],
                [
                    (
                        date.fromisoformat(row.start_date),
                        date.fromisoformat(row.end_date),
                    )
                    for row in target_rows
                ],
            )
            non_derivable = sorted(
                row.report_id
                for row, res in zip(target_rows, results, strict=True)
                if not res.unique
            )
            if non_derivable:
                # 验算失败：不做任何状态变更。
                raise ArchiveConditionError(
                    non_derivable,
                    "整批移出后，剩余在线报表无法唯一推出上述报表的档期总量，"
                    "本批归档请求被拒绝，报表状态不变",
                )

            # 3) 条件成立 -> 原子落库。
            try:
                for row in target_rows:
                    row.status = STATUS_ARCHIVED
                session.commit()
            except Exception:
                session.rollback()
                raise
            self._invalidate()

            online_count = session.scalar(
                select(func.count())
                .select_from(ReportRow)
                .where(ReportRow.status == STATUS_ONLINE)
            )
            archived_count = session.scalar(
                select(func.count())
                .select_from(ReportRow)
                .where(ReportRow.status == STATUS_ARCHIVED)
            )
            return {
                "archived_ids": ids,
                "online_count": online_count,
                "archived_count": archived_count,
            }

    # ------------------------------------------------------------------ #
    # 查数（只用同一套在线报表；归档件不参与）
    # ------------------------------------------------------------------ #
    def query_daily(
        self,
        session: Session,
        start: date | None,
        end: date | None,
    ) -> list[dict]:
        solution = self.current_solution(session)
        if start is None or end is None:
            if not solution.reports:
                return []
            start = start or min(r.start_date for r in solution.reports)
            end = end or max(r.end_date for r in solution.reports)
        if end < start:
            raise ReportValidationError("查询区间末日早于首天")

        covered = set()
        for r in solution.reports:
            cur = max(r.start_date, start)
            stop = min(r.end_date, end)
            while cur <= stop:
                covered.add(cur)
                cur += timedelta(days=1)

        out: list[dict] = []
        cur = start
        while cur <= end:
            if cur in solution.determined:
                out.append(
                    {
                        "day": cur,
                        "status": "determined",
                        "visitors": solution.determined[cur],
                        "covered": True,
                    }
                )
            elif cur in covered:
                out.append(
                    {"day": cur, "status": "pending", "visitors": None, "covered": True}
                )
            else:
                out.append(
                    {"day": cur, "status": "pending", "visitors": None, "covered": False}
                )
            cur += timedelta(days=1)
        return out
