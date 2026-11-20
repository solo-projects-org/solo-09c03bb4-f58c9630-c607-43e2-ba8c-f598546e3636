"""请求 / 响应模式。"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

from .database import STATUS_ARCHIVED, STATUS_ONLINE


class ReportIn(BaseModel):
    report_id: str = Field(..., examples=["R2024-001"], description="报表编号，唯一")
    start_date: date = Field(..., description="首天（计入），ISO 日期")
    end_date: date = Field(..., description="末日（计入，不早于首天），ISO 日期")
    total: int = Field(..., ge=0, description="档期总人次，非负整数")

    @field_validator("report_id")
    @classmethod
    def _id_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("报表编号不能为空")
        return v.strip()


class BatchUploadIn(BaseModel):
    reports: list[ReportIn] = Field(..., min_length=1, description="本批补录的报表")


class ReportOut(BaseModel):
    report_id: str
    start_date: date
    end_date: date
    total: int
    version: int
    status: str = Field(
        ...,
        description=(
            f"{STATUS_ONLINE}=在线（参与查数）；{STATUS_ARCHIVED}=归档（留档备查，"
            "不参与在线查数，其总量仍可由在线报表唯一推出）"
        ),
    )


class BatchUploadOut(BaseModel):
    accepted: bool
    stored_count: int = Field(..., description="入库后报表总数（含归档件，上限 120）")
    online_count: int = Field(..., description="入库后在线报表数量")
    archived_count: int = Field(..., description="入库后归档报表数量")
    replaced_ids: list[str] = Field(default_factory=list, description="本批整份替换的编号")
    inserted_ids: list[str] = Field(default_factory=list, description="本批新增的编号")
    restored_ids: list[str] = Field(
        default_factory=list,
        description="补录后失去在线依据、随补报从归档恢复为在线的编号",
    )
    reports: list[ReportOut] = Field(default_factory=list, description="入库后该批报表视图")


class ConflictOut(BaseModel):
    accepted: bool = False
    conflict_report_ids: list[str] = Field(
        ..., description="一组冲突报表编号；去掉组内任一份后其余即可自洽"
    )
    reason: str


class ArchiveIn(BaseModel):
    report_ids: list[str] = Field(
        ...,
        min_length=1,
        description="本批申请归档的在线报表编号；整批同时成立才会移出在线集合",
    )

    @field_validator("report_ids")
    @classmethod
    def _ids_not_blank(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values]
        if any(not v for v in cleaned):
            raise ValueError("报表编号不能为空")
        return cleaned


class ArchiveOut(BaseModel):
    accepted: bool
    archived_ids: list[str] = Field(default_factory=list, description="本批转为归档的编号")
    online_count: int = Field(..., description="归档后在线报表数量")
    archived_count: int = Field(..., description="归档后归档报表数量（含本批）")


class ArchiveRejectedOut(BaseModel):
    accepted: bool = False
    non_derivable_report_ids: list[str] = Field(
        ...,
        description="移出整批后，剩余在线报表无法唯一推出其档期总量的报表编号",
    )
    reason: str


class DailyValueOut(BaseModel):
    day: date
    status: str = Field(..., description="determined=唯一确定；pending=待补")
    visitors: int | None = None
    covered: bool = Field(..., description="该日是否被至少一份在线报表覆盖")


class DailyQueryOut(BaseModel):
    start_date: date
    end_date: date
    days: list[DailyValueOut]
    determined_count: int
    pending_count: int
