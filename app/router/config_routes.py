"""
配置路由模块
"""

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.core.security import verify_auth_token
from app.log.logger import Logger, get_config_routes_logger
from app.service.config.config_service import ConfigService
from app.utils.helpers import redact_key_for_logging

router = APIRouter(prefix="/api/config", tags=["config"])

logger = get_config_routes_logger()


@router.get("", response_model=Dict[str, Any])
async def get_config(request: Request):
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning("Unauthorized access attempt to config page")
        return RedirectResponse(url="/", status_code=302)
    return await ConfigService.get_config()


@router.put("", response_model=Dict[str, Any])
async def update_config(config_data: Dict[str, Any], request: Request):
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning("Unauthorized access attempt to config page")
        return RedirectResponse(url="/", status_code=302)
    try:
        result = await ConfigService.update_config(config_data)
        # 配置更新成功后，立即更新所有 logger 的级别
        Logger.update_log_levels(config_data["LOG_LEVEL"])
        logger.info("Log levels updated after configuration change.")
        return result
    except Exception as e:
        logger.error(f"Error updating config or log levels: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reset", response_model=Dict[str, Any])
async def reset_config(request: Request):
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning("Unauthorized access attempt to config page")
        return RedirectResponse(url="/", status_code=302)
    try:
        return await ConfigService.reset_config()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class DeleteKeysRequest(BaseModel):
    keys: List[str] = Field(..., description="List of API keys to delete")


class BatchKeysRequest(BaseModel):
    keys: List[str] = Field(..., description="List of API keys to operate on")


@router.delete("/keys/{key_to_delete}", response_model=Dict[str, Any])
async def delete_single_key(key_to_delete: str, request: Request):
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning(f"Unauthorized attempt to delete key: {redact_key_for_logging(key_to_delete)}")
        return RedirectResponse(url="/", status_code=302)
    try:
        logger.info(f"Attempting to delete key: {redact_key_for_logging(key_to_delete)}")
        result = await ConfigService.delete_key(key_to_delete)
        if not result.get("success"):
            raise HTTPException(
                status_code=(
                    404 if "not found" in result.get("message", "").lower() else 400
                ),
                detail=result.get("message"),
            )
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting key '{redact_key_for_logging(key_to_delete)}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error deleting key: {str(e)}")


@router.post("/keys/delete-selected", response_model=Dict[str, Any])
async def delete_selected_keys_route(
    delete_request: DeleteKeysRequest, request: Request
):
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning("Unauthorized attempt to bulk delete keys")
        return RedirectResponse(url="/", status_code=302)

    if not delete_request.keys:
        logger.warning("Attempt to bulk delete keys with an empty list.")
        raise HTTPException(status_code=400, detail="No keys provided for deletion.")

    try:
        logger.info(f"Attempting to bulk delete {len(delete_request.keys)} keys.")
        result = await ConfigService.delete_selected_keys(delete_request.keys)
        if not result.get("success") and result.get("deleted_count", 0) == 0:
            raise HTTPException(
                status_code=400, detail=result.get("message", "Failed to delete keys.")
            )
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error bulk deleting keys: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error bulk deleting keys: {str(e)}"
        )


@router.get("/ui/models")
async def get_ui_models(request: Request):
    auth_token_cookie = request.cookies.get("auth_token")
    if not auth_token_cookie or not verify_auth_token(auth_token_cookie):
        logger.warning("Unauthorized access attempt to /api/config/ui/models")
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        models = await ConfigService.fetch_ui_models()
        return models
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Unexpected error in /ui/models endpoint: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred while fetching UI models: {str(e)}",
        )


@router.post("/keys/{key}/enable", response_model=Dict[str, Any])
async def enable_single_key(key: str, request: Request):
    """启用单个密钥"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning(f"Unauthorized attempt to enable key: {key}")
        return RedirectResponse(url="/", status_code=302)
    try:
        logger.info(f"Attempting to enable key: {key}")
        result = await ConfigService.enable_key(key)
        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=result.get("message", "Failed to enable key"),
            )
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error enabling key '{key}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error enabling key: {str(e)}")


@router.post("/keys/{key}/disable", response_model=Dict[str, Any])
async def disable_single_key(key: str, request: Request):
    """禁用单个密钥"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning(f"Unauthorized attempt to disable key: {key}")
        return RedirectResponse(url="/", status_code=302)
    try:
        logger.info(f"Attempting to disable key: {key}")
        result = await ConfigService.disable_key(key)
        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=result.get("message", "Failed to disable key"),
            )
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error disabling key '{key}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error disabling key: {str(e)}")


@router.post("/keys/batch-enable", response_model=Dict[str, Any])
async def batch_enable_keys_route(
    batch_request: BatchKeysRequest, request: Request
):
    """批量启用密钥"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning("Unauthorized attempt to batch enable keys")
        return RedirectResponse(url="/", status_code=302)

    if not batch_request.keys:
        logger.warning("Attempt to batch enable keys with an empty list.")
        raise HTTPException(status_code=400, detail="No keys provided for enabling.")

    try:
        logger.info(f"Attempting to batch enable {len(batch_request.keys)} keys.")
        result = await ConfigService.batch_enable_keys(batch_request.keys)
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error batch enabling keys: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error batch enabling keys: {str(e)}"
        )


@router.post("/keys/batch-disable", response_model=Dict[str, Any])
async def batch_disable_keys_route(
    batch_request: BatchKeysRequest, request: Request
):
    """批量禁用密钥"""
    auth_token = request.cookies.get("auth_token")
    if not auth_token or not verify_auth_token(auth_token):
        logger.warning("Unauthorized attempt to batch disable keys")
        return RedirectResponse(url="/", status_code=302)

    if not batch_request.keys:
        logger.warning("Attempt to batch disable keys with an empty list.")
        raise HTTPException(status_code=400, detail="No keys provided for disabling.")

    try:
        logger.info(f"Attempting to batch disable {len(batch_request.keys)} keys.")
        result = await ConfigService.batch_disable_keys(batch_request.keys)
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error batch disabling keys: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Error batch disabling keys: {str(e)}"
        )
