"""业务服务层：整批原子提交、冲突诊断、求解缓存。"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import solver
from .database import ReportRow
from .solver import MAX_REPORT_COUNT, Report, ReportValidationError, SolvedSystem


class CapacityExceededError(Exception):
    """现行报表数量超过 120 份上限。"""


class BatchConflictError(Exception):
    """整批与已生效报表无法同时成立。"""

    def __init__(self, conflict_ids: list[str], reason: str):
        self.conflict_ids = conflict_ids
        self.reason = reason
        super().__init__(reason)


class ReportService:
    """所有写操作经同一把锁串行化；读操作走快照 + 版本缓存。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: SolvedSystem | None = None  # 现行报表求解结果，提交后失效

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

    def _load_all(self, session: Session) -> list[Report]:
        rows = session.scalars(select(ReportRow).order_by(ReportRow.report_id)).all()
        return [self._row_to_report(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 求解缓存（只依赖当前库里的报表内容版本）
    # ------------------------------------------------------------------ #
    def current_solution(self, session: Session) -> SolvedSystem:
        with self._lock:
            if self._cache is None:
                self._cache = solver.solve(self._load_all(session))
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

            # 2) 120 份容量护栏（替换不占新名额）。
            projected_count = len(existing_rows) + len(inserted_ids)
            if projected_count > MAX_REPORT_COUNT:
                raise CapacityExceededError(
                    f"入库后将有 {projected_count} 份报表，超过最多 {MAX_REPORT_COUNT} 份上限"
                )

            # 3) 用“替换后的整份视图”拼出候选集合，再与现行其余报表核对。
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
                    "本批报表与已生效报表无法同时成立，整批退回；"
                    "去掉冲突组内任意一份后其余报表可自洽",
                )

            # 5) 全部通过 -> 原子落库（同事务，异常整体回滚）。
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
                            )
                        )
                    else:
                        row.start_date = r.start_date.isoformat()
                        row.end_date = r.end_date.isoformat()
                        row.total = r.total
                        row.version += 1
                session.commit()
            except Exception:
                session.rollback()
                raise
            self._invalidate()

            final_rows = session.scalars(
                select(ReportRow).where(ReportRow.report_id.in_(ids_in_batch))
            ).all()
            return {
                "stored_count": session.scalar(
                    select(func.count()).select_from(ReportRow)
                ),
                "replaced_ids": sorted(replaced_ids),
                "inserted_ids": sorted(inserted_ids),
                "rows": final_rows,
            }

    # ------------------------------------------------------------------ #
    # 查数
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
