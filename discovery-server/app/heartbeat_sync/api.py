"""alive-sync 管理维护 API（/admin/alive-sync）。

提供运营/调试用的状态查看与手动重同步接口，不对外暴露于生产流量路径。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.heartbeat_sync.runtime import get_alive_sync_service

router = APIRouter()


@router.get("/status", summary="查询 alive-sync 服务状态")
async def get_alive_sync_status() -> dict[str, Any]:
    """返回 alive-sync 后台服务当前状态。"""
    service = get_alive_sync_service()
    if service is None:
        return {"running": False, "message": "alive-sync 服务未启动（未启用或守卫条件不满足）"}
    return await service.status()


@router.post("/resync", summary="触发手动重同步")
async def trigger_resync() -> dict[str, Any]:
    """触发 alive-sync 重同步（reset + bootstrap）。"""
    service = get_alive_sync_service()
    if service is None:
        raise HTTPException(status_code=503, detail="alive-sync 服务未启动")
    await service.request_resync("admin_manual_trigger")
    return {"message": "重同步已触发"}
