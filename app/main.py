"""FastAPI 应用入口与路由。"""
from __future__ import annotations

import sqlite3
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, status
from fastapi.responses import JSONResponse, RedirectResponse

from . import database, engine
from .models import SimulationList, SimulationRequest, SimulationResult


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时建表（不存在则创建），避免首次请求才暴露路径权限问题
    conn = database.connect()
    conn.close()
    yield


app = FastAPI(
    title="实验室样本混样追踪 API",
    description=(
        "提交初始样本（体积 + 标记物浓度）和按时间排序的转移操作，"
        "服务逐步计算混合浓度与来源占比，并对含移液损耗（可选 "
        "delivered_amount）的系统按“所有管内 + 累计 waste”口径做守恒校验，"
        "每次模拟记录持久化到 SQLite。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_db() -> sqlite3.Connection:
    conn = database.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["系统"])
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/simulations",
    tags=["模拟"],
    response_model=SimulationResult,
    responses={
        201: {"model": SimulationResult, "description": "模拟成功并已保存"},
        400: {"model": SimulationResult, "description": "业务规则失败（超量 / 时间逆序 / 未知样本）"},
        422: {"description": "请求体格式 / 字段校验失败（FastAPI 标准错误结构）"},
    },
    status_code=status.HTTP_201_CREATED,
    summary="提交一次混样模拟",
)
def create_simulation(
    request: SimulationRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """执行模拟并落库。

    返回 ``201`` 表示业务模拟成功；当转移超量、时间逆序或引用未知样本等
    业务规则失败时返回 ``400``，响应体中 ``status=failed`` 且 ``error``
    给出错误码、可读信息和精确定位（如 ``transfers[2].source``）。
    """
    result = engine.run_simulation(request)
    record = database.insert_simulation(db, result)

    http_status = (
        status.HTTP_201_CREATED
        if record["status"] == "success"
        else status.HTTP_400_BAD_REQUEST
    )
    return JSONResponse(content=record, status_code=http_status)


@app.get(
    "/api/simulations/{sim_id}",
    tags=["模拟"],
    response_model=SimulationResult,
    responses={
        400: {"model": SimulationResult, "description": "该次模拟的业务结果为失败"},
        404: {"description": "模拟记录不存在"},
    },
    summary="查询某次模拟的完整结果",
)
def get_simulation(
    sim_id: str, db: sqlite3.Connection = Depends(get_db)
) -> JSONResponse:
    record = database.get_simulation(db, sim_id)
    if record is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "detail": {
                    "code": "SIMULATION_NOT_FOUND",
                    "message": f"未找到 id 为 {sim_id!r} 的模拟记录",
                    "location": f"path.sim_id",
                }
            },
        )
    http_status = (
        status.HTTP_200_OK
        if record["status"] == "success"
        else status.HTTP_400_BAD_REQUEST
    )
    return JSONResponse(content=record, status_code=http_status)


@app.get(
    "/api/simulations",
    tags=["模拟"],
    response_model=SimulationList,
    summary="列出历史模拟记录（按创建时间倒序）",
)
def list_simulations(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        pattern="^(success|failed)$",
        description="按状态过滤：success 或 failed",
    ),
    db: sqlite3.Connection = Depends(get_db),
) -> SimulationList:
    items = database.list_simulations(db, limit=limit, offset=offset)
    if status_filter:
        items = [item for item in items if item["status"] == status_filter]
    return SimulationList(items=items, limit=limit, offset=offset)
