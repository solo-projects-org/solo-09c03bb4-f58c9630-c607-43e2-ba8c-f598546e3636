"""FastAPI 入口：报表补仓 / 归档 / 查数接口，自带 Swagger 文档。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schemas
from .database import STATUS_ARCHIVED, STATUS_ONLINE, ReportRow, get_session, init_db
from .service import (
    ArchiveConditionError,
    BatchConflictError,
    CapacityExceededError,
    ReportService,
)
from .solver import MAX_REPORT_COUNT, MAX_SPAN_DAYS, Report, ReportValidationError

service = ReportService()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="景区客流报表补仓 API",
    version="1.1.0",
    lifespan=lifespan,
    description=(
        "留存跨日客流报表的补录、归档与日明细查数。\n\n"
        f"- 每份报表档期最长 {MAX_SPAN_DAYS} 天，库内最多 {MAX_REPORT_COUNT} 份（含归档件）；\n"
        "- 同编号整份替换并转为在线；整批与在线及归档报表一起核对，冲突则整批退回；\n"
        "- 归档只移走“总量仍能被剩余在线报表唯一推出”的在线报表，原报表留档备查；\n"
        "- 日明细查数只用同一套在线报表；补录后失去依据的归档件自动恢复在线。"
    ),
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 报表管理
# --------------------------------------------------------------------------- #
def _to_model(inp: schemas.ReportIn) -> Report:
    return Report(
        report_id=inp.report_id,
        start_date=inp.start_date,
        end_date=inp.end_date,
        total=inp.total,
    )


def _row_to_out(row: ReportRow) -> schemas.ReportOut:
    return schemas.ReportOut(
        report_id=row.report_id,
        start_date=date.fromisoformat(row.start_date),
        end_date=date.fromisoformat(row.end_date),
        total=row.total,
        version=row.version,
        status=row.status,
    )


@app.post(
    "/reports/batch",
    tags=["报表"],
    summary="整批补录报表（同编号整份替换并转在线，冲突整批退回）",
    response_model=schemas.BatchUploadOut,
    status_code=200,
    responses={
        409: {"model": schemas.ConflictOut, "description": "报表间无法同时成立"},
        422: {"description": "单份报表非法 / 批次为空 / 超过 120 份上限（含归档件）"},
    },
)
def upload_batch(
    payload: schemas.BatchUploadIn,
    session: Annotated[Session, Depends(get_session)],
) -> schemas.BatchUploadOut:
    incoming = [_to_model(r) for r in payload.reports]
    try:
        result = service.upload_batch(session, incoming)
    except ReportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CapacityExceededError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BatchConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "accepted": False,
                "conflict_report_ids": exc.conflict_ids,
                "reason": exc.reason,
            },
        ) from exc

    return schemas.BatchUploadOut(
        accepted=True,
        stored_count=result["stored_count"],
        online_count=result["online_count"],
        archived_count=result["archived_count"],
        replaced_ids=result["replaced_ids"],
        inserted_ids=result["inserted_ids"],
        restored_ids=result["restored_ids"],
        reports=[_row_to_out(row) for row in result["rows"]],
    )


@app.post(
    "/reports/archive",
    tags=["报表"],
    summary="整批归档在线报表（剩余在线报表须仍能唯一推出每份原件总量）",
    response_model=schemas.ArchiveOut,
    status_code=200,
    responses={
        409: {
            "model": schemas.ArchiveRejectedOut,
            "description": "条件不成立：列出无法由剩余在线报表推出总量的报表编号",
        },
        422: {"description": "批次为空 / 编号不存在或已归档 / 批内重复"},
    },
)
def archive_batch(
    payload: schemas.ArchiveIn,
    session: Annotated[Session, Depends(get_session)],
) -> schemas.ArchiveOut:
    try:
        result = service.archive_batch(session, payload.report_ids)
    except ReportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ArchiveConditionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "accepted": False,
                "non_derivable_report_ids": exc.non_derivable_ids,
                "reason": exc.reason,
            },
        ) from exc

    return schemas.ArchiveOut(
        accepted=True,
        archived_ids=result["archived_ids"],
        online_count=result["online_count"],
        archived_count=result["archived_count"],
    )


@app.get(
    "/reports",
    tags=["报表"],
    summary="列出报表原件（标明在线 / 归档状态）",
    response_model=list[schemas.ReportOut],
)
def list_reports(
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[
        str | None,
        Query(
            description=(
                f"状态过滤：{STATUS_ONLINE}=仅在线，{STATUS_ARCHIVED}=仅归档；"
                "缺省返回全部"
            )
        ),
    ] = None,
) -> list[schemas.ReportOut]:
    stmt = select(ReportRow).order_by(ReportRow.report_id)
    if status is not None:
        if status not in (STATUS_ONLINE, STATUS_ARCHIVED):
            raise HTTPException(
                status_code=422,
                detail=f"非法状态过滤值 {status!r}，可选 {STATUS_ONLINE} / {STATUS_ARCHIVED}",
            )
        stmt = stmt.where(ReportRow.status == status)
    rows = session.scalars(stmt).all()
    return [_row_to_out(row) for row in rows]


# --------------------------------------------------------------------------- #
# 查数（只用在线报表）
# --------------------------------------------------------------------------- #
@app.get(
    "/daily",
    tags=["查数"],
    summary="按日期区间查询日人次（只用在线报表；无法唯一确定的日期标为待补）",
    response_model=schemas.DailyQueryOut,
)
def query_daily(
    session: Annotated[Session, Depends(get_session)],
    start_date: Annotated[
        date | None, Query(description="首天（计入）；缺省取所有在线报表最早首天")
    ] = None,
    end_date: Annotated[
        date | None, Query(description="末日（计入）；缺省取所有在线报表最晚末日")
    ] = None,
) -> schemas.DailyQueryOut:
    try:
        days = service.query_daily(session, start_date, end_date)
    except ReportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not days:
        raise HTTPException(
            status_code=404,
            detail="库内没有任何在线报表，且未指定查询区间；请先补录报表或显式传入日期区间",
        )
    determined = sum(1 for d in days if d["status"] == "determined")
    return schemas.DailyQueryOut(
        start_date=days[0]["day"],
        end_date=days[-1]["day"],
        days=[schemas.DailyValueOut(**d) for d in days],
        determined_count=determined,
        pending_count=len(days) - determined,
    )
