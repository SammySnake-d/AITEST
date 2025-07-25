from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from copy import deepcopy
import asyncio
from app.config.config import settings
from app.log.logger import get_gemini_logger
from app.core.security import SecurityService
from app.domain.gemini_models import (
    GeminiContent, GeminiRequest, ResetSelectedKeysRequest, VerifySelectedKeysRequest,
    BatchSearchKeysRequest, BatchOperationKeysRequest, KeyFreezeRequest,
    KeysPaginationRequest, KeysPaginationResponse
)
from pydantic import BaseModel
from typing import Optional, List
from app.service.chat.gemini_chat_service import GeminiChatService
from app.service.key.key_manager import KeyManager, get_key_manager_instance
from app.service.tts.native.tts_routes import get_tts_chat_service
from app.service.model.model_service import ModelService
from app.handler.retry_handler import RetryHandler
from app.handler.error_handler import handle_route_errors
from app.core.constants import API_VERSION
from app.utils.helpers import redact_key_for_logging

router = APIRouter(prefix=f"/gemini/{API_VERSION}")
router_v1beta = APIRouter(prefix=f"/{API_VERSION}")
logger = get_gemini_logger()

security_service = SecurityService()
model_service = ModelService()


async def get_key_manager():
    """获取密钥管理器实例"""
    return await get_key_manager_instance()


async def get_next_working_key(key_manager: KeyManager = Depends(get_key_manager)):
    """获取下一个可用的API密钥"""
    return await key_manager.get_next_working_key()


async def get_chat_service(key_manager: KeyManager = Depends(get_key_manager)):
    """获取Gemini聊天服务实例"""
    return GeminiChatService(settings.BASE_URL, key_manager)


@router.get("/models")
@router_v1beta.get("/models")
async def list_models(
    _=Depends(security_service.verify_key_or_goog_api_key),
    key_manager: KeyManager = Depends(get_key_manager)
):
    """获取可用的 Gemini 模型列表，并根据配置添加衍生模型（搜索、图像、非思考）。"""
    operation_name = "list_gemini_models"
    logger.info("-" * 50 + operation_name + "-" * 50)
    logger.info("Handling Gemini models list request")

    try:
        api_key = await key_manager.get_first_valid_key()
        if not api_key:
            raise HTTPException(status_code=503, detail="No valid API keys available to fetch models.")
        logger.info(f"Using API key: {redact_key_for_logging(api_key)}")

        models_data = await model_service.get_gemini_models(api_key)
        if not models_data or "models" not in models_data:
            raise HTTPException(status_code=500, detail="Failed to fetch base models list.")

        models_json = deepcopy(models_data)
        model_mapping = {x.get("name", "").split("/", maxsplit=1)[-1]: x for x in models_json.get("models", [])}

        def add_derived_model(base_name, suffix, display_suffix):
            model = model_mapping.get(base_name)
            if not model:
                logger.warning(f"Base model '{base_name}' not found for derived model '{suffix}'.")
                return
            item = deepcopy(model)
            item["name"] = f"models/{base_name}{suffix}"
            display_name = f'{item.get("displayName", base_name)}{display_suffix}'
            item["displayName"] = display_name
            item["description"] = display_name
            models_json["models"].append(item)

        if settings.SEARCH_MODELS:
            for name in settings.SEARCH_MODELS:
                add_derived_model(name, "-search", " For Search")
        if settings.IMAGE_MODELS:
            for name in settings.IMAGE_MODELS:
                 add_derived_model(name, "-image", " For Image")
        if settings.THINKING_MODELS:
            for name in settings.THINKING_MODELS:
                add_derived_model(name, "-non-thinking", " Non Thinking")

        logger.info("Gemini models list request successful")
        return models_json
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Error getting Gemini models list: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Internal server error while fetching Gemini models list"
        ) from e


@router.post("/models/{model_name}:generateContent")
@router_v1beta.post("/models/{model_name}:generateContent")
@RetryHandler(key_arg="api_key")
async def generate_content(
    model_name: str,
    request: GeminiRequest,
    _=Depends(security_service.verify_key_or_goog_api_key),
    api_key: str = Depends(get_next_working_key),
    key_manager: KeyManager = Depends(get_key_manager),
    chat_service: GeminiChatService = Depends(get_chat_service)
):
    """处理 Gemini 非流式内容生成请求。"""
    operation_name = "gemini_generate_content"
    async with handle_route_errors(logger, operation_name, failure_message="Content generation failed"):
        logger.info(f"Handling Gemini content generation request for model: {model_name}")
        logger.debug(f"Request: \n{request.model_dump_json(indent=2)}")

        # 检测是否为原生Gemini TTS请求
        is_native_tts = False
        if "tts" in model_name.lower() and request.generationConfig:
            # 直接从解析后的request对象获取TTS配置
            response_modalities = request.generationConfig.responseModalities or []
            speech_config = request.generationConfig.speechConfig or {}

            # 如果包含AUDIO模态和语音配置，则认为是原生TTS请求
            if "AUDIO" in response_modalities and speech_config:
                is_native_tts = True
                logger.info("Detected native Gemini TTS request")
                logger.info(f"TTS responseModalities: {response_modalities}")
                logger.info(f"TTS speechConfig: {speech_config}")

        logger.info(f"Using API key: {redact_key_for_logging(api_key)}")

        if not await model_service.check_model_support(model_name):
            raise HTTPException(status_code=400, detail=f"Model {model_name} is not supported")

        # 所有原生TTS请求都使用TTS增强服务
        if is_native_tts:
            try:
                logger.info("Using native TTS enhanced service")
                tts_service = await get_tts_chat_service(key_manager)
                response = await tts_service.generate_content(
                    model=model_name,
                    request=request,
                    api_key=api_key
                )
                return response
            except Exception as e:
                logger.warning(f"Native TTS processing failed, falling back to standard service: {e}")

        # 使用标准服务处理所有其他请求（非TTS）
        response = await chat_service.generate_content(
            model=model_name,
            request=request,
            api_key=api_key
        )
        return response


@router.post("/models/{model_name}:streamGenerateContent")
@router_v1beta.post("/models/{model_name}:streamGenerateContent")
@RetryHandler(key_arg="api_key")
async def stream_generate_content(
    model_name: str,
    request: GeminiRequest,
    _=Depends(security_service.verify_key_or_goog_api_key),
    api_key: str = Depends(get_next_working_key),
    key_manager: KeyManager = Depends(get_key_manager),
    chat_service: GeminiChatService = Depends(get_chat_service)
):
    """处理 Gemini 流式内容生成请求。"""
    operation_name = "gemini_stream_generate_content"
    async with handle_route_errors(logger, operation_name, failure_message="Streaming request initiation failed"):
        logger.info(f"Handling Gemini streaming content generation for model: {model_name}")
        logger.debug(f"Request: \n{request.model_dump_json(indent=2)}")
        logger.info(f"Using API key: {redact_key_for_logging(api_key)}")

        if not await model_service.check_model_support(model_name):
            raise HTTPException(status_code=400, detail=f"Model {model_name} is not supported")

        response_stream = chat_service.stream_generate_content(
            model=model_name,
            request=request,
            api_key=api_key
        )
        return StreamingResponse(response_stream, media_type="text/event-stream")


@router.post("/models/{model_name}:countTokens")
@router_v1beta.post("/models/{model_name}:countTokens")
@RetryHandler(key_arg="api_key")
async def count_tokens(
    model_name: str,
    request: GeminiRequest,
    _=Depends(security_service.verify_key_or_goog_api_key),
    api_key: str = Depends(get_next_working_key),
    key_manager: KeyManager = Depends(get_key_manager),
    chat_service: GeminiChatService = Depends(get_chat_service)
):
    """处理 Gemini token 计数请求。"""
    operation_name = "gemini_count_tokens"
    async with handle_route_errors(logger, operation_name, failure_message="Token counting failed"):
        logger.info(f"Handling Gemini token count request for model: {model_name}")
        logger.debug(f"Request: \n{request.model_dump_json(indent=2)}")
        logger.info(f"Using API key: {redact_key_for_logging(api_key)}")

        if not await model_service.check_model_support(model_name):
            raise HTTPException(status_code=400, detail=f"Model {model_name} is not supported")

        response = await chat_service.count_tokens(
            model=model_name,
            request=request,
            api_key=api_key
        )
        return response


@router.post("/reset-all-fail-counts")
async def reset_all_key_fail_counts(key_type: str = None, key_manager: KeyManager = Depends(get_key_manager)):
    """批量重置Gemini API密钥的失败计数，可选择性地仅重置有效或无效密钥"""
    logger.info("-" * 50 + "reset_all_gemini_key_fail_counts" + "-" * 50)
    logger.info(f"Received reset request with key_type: {key_type}")
    
    try:
        # 获取分类后的密钥
        keys_by_status = await key_manager.get_keys_by_status()
        valid_keys = keys_by_status.get("valid_keys", {})
        invalid_keys = keys_by_status.get("invalid_keys", {})
        disabled_keys = keys_by_status.get("disabled_keys", {})

        # 根据类型选择要重置的密钥
        keys_to_reset = []
        if key_type == "valid":
            keys_to_reset = list(valid_keys.keys())
            logger.info(f"Resetting only valid keys, count: {len(keys_to_reset)}")
        elif key_type == "invalid":
            keys_to_reset = list(invalid_keys.keys())
            logger.info(f"Resetting only invalid keys, count: {len(keys_to_reset)}")
        elif key_type == "disabled":
            keys_to_reset = list(disabled_keys.keys())
            logger.info(f"Resetting only disabled keys, count: {len(keys_to_reset)}")
        else:
            # 重置所有密钥
            await key_manager.reset_failure_counts()
            return JSONResponse({"success": True, "message": "所有密钥的失败计数已重置"})
        
        # 批量重置指定类型的密钥
        for key in keys_to_reset:
            await key_manager.reset_key_failure_count(key)
        
        return JSONResponse({
            "success": True,
            "message": f"{key_type}密钥的失败计数已重置",
            "reset_count": len(keys_to_reset)
        })
    except Exception as e:
        logger.error(f"Failed to reset key failure counts: {str(e)}")
        return JSONResponse({"success": False, "message": f"批量重置失败: {str(e)}"}, status_code=500)
    
    
@router.post("/reset-selected-fail-counts")
async def reset_selected_key_fail_counts(
    request: ResetSelectedKeysRequest,
    key_manager: KeyManager = Depends(get_key_manager)
):
    """批量重置选定Gemini API密钥的失败计数"""
    logger.info("-" * 50 + "reset_selected_gemini_key_fail_counts" + "-" * 50)
    keys_to_reset = request.keys
    key_type = request.key_type
    logger.info(f"Received reset request for {len(keys_to_reset)} selected {key_type} keys.")

    if not keys_to_reset:
        return JSONResponse({"success": False, "message": "没有提供需要重置的密钥"}, status_code=400)

    reset_count = 0
    errors = []

    try:
        for key in keys_to_reset:
            try:
                result = await key_manager.reset_key_failure_count(key)
                if result:
                    reset_count += 1
                else:
                    logger.warning(f"Key not found during selective reset: {redact_key_for_logging(key)}")
            except Exception as key_error:
                logger.error(f"Error resetting key {redact_key_for_logging(key)}: {str(key_error)}")
                errors.append(f"Key {key}: {str(key_error)}")

        if errors:
             error_message = f"批量重置完成，但出现错误: {'; '.join(errors)}"
             final_success = reset_count > 0
             status_code = 207 if final_success and errors else 500
             return JSONResponse({
                 "success": final_success,
                 "message": error_message,
                 "reset_count": reset_count
             }, status_code=status_code)

        return JSONResponse({
            "success": True,
            "message": f"成功重置 {reset_count} 个选定 {key_type} 密钥的失败计数",
            "reset_count": reset_count
        })
    except Exception as e:
        logger.error(f"Failed to process reset selected key failure counts request: {str(e)}")
        return JSONResponse({"success": False, "message": f"批量重置处理失败: {str(e)}"}, status_code=500)


@router.post("/reset-fail-count/{api_key}")
async def reset_key_fail_count(api_key: str, key_manager: KeyManager = Depends(get_key_manager)):
    """重置指定Gemini API密钥的失败计数"""
    logger.info("-" * 50 + "reset_gemini_key_fail_count" + "-" * 50)
    logger.info(f"Resetting failure count for API key: {redact_key_for_logging(api_key)}")
    
    try:
        result = await key_manager.reset_key_failure_count(api_key)
        if result:
            return JSONResponse({"success": True, "message": "失败计数已重置"})
        return JSONResponse({"success": False, "message": "未找到指定密钥"}, status_code=404)
    except Exception as e:
        logger.error(f"Failed to reset key failure count: {str(e)}")
        return JSONResponse({"success": False, "message": f"重置失败: {str(e)}"}, status_code=500)


@router.post("/verify-key/{api_key}")
async def verify_key(api_key: str, chat_service: GeminiChatService = Depends(get_chat_service), key_manager: KeyManager = Depends(get_key_manager)):
    """验证Gemini API密钥的有效性"""
    logger.info("-" * 50 + "verify_gemini_key" + "-" * 50)
    logger.info("Verifying API key validity")
    
    try:
        gemini_request = GeminiRequest(
            contents=[
                GeminiContent(
                    role="user",
                    parts=[{"text": "hi"}],
                )
            ],
            generation_config={"temperature": 0.7, "topP": 1.0, "maxOutputTokens": 10}
        )
        
        response = await chat_service.generate_content(
            settings.TEST_MODEL,
            gemini_request,
            api_key
        )
        
        if response:
            # 如果密钥验证成功，则重置其失败计数
            await key_manager.reset_key_failure_count(api_key)
            return JSONResponse({"status": "valid"})
    except Exception as e:
        error_message = str(e)
        logger.error(f"Key verification failed: {error_message}")

        # 检查是否是429错误，如果是则调用专门的429错误处理机制
        is_429_error = "429" in error_message or "Too Many Requests" in error_message or "quota" in error_message.lower()

        if is_429_error and settings.ENABLE_KEY_FREEZE_ON_429:
            # 对于429错误，冷冻密钥而不是增加失败计数
            await key_manager.handle_429_error(api_key)
            logger.info(f"Single verification: Key {redact_key_for_logging(api_key)} frozen due to 429 error")
        else:
            # 对于其他错误，使用正常的失败处理逻辑
            async with key_manager.failure_count_lock:
                if api_key in key_manager.key_failure_counts:
                    key_manager.key_failure_counts[api_key] += 1
                    logger.warning(f"Verification exception for key: {redact_key_for_logging(api_key)}, incrementing failure count")
                else:
                    key_manager.key_failure_counts[api_key] = 1
                    logger.warning(f"Verification exception for key: {redact_key_for_logging(api_key)}, initializing failure count to 1")

        return JSONResponse({"status": "invalid", "error": error_message})


@router.post("/verify-selected-keys")
async def verify_selected_keys(
    request: VerifySelectedKeysRequest,
    chat_service: GeminiChatService = Depends(get_chat_service),
    key_manager: KeyManager = Depends(get_key_manager)
):
    """批量验证选定Gemini API密钥的有效性"""
    logger.info("-" * 50 + "verify_selected_gemini_keys" + "-" * 50)
    keys_to_verify = request.keys
    logger.info(f"Received verification request for {len(keys_to_verify)} selected keys.")

    if not keys_to_verify:
        return JSONResponse({"success": False, "message": "没有提供需要验证的密钥"}, status_code=400)

    successful_keys = []
    failed_keys = {}

    async def _verify_single_key(api_key: str):
        """内部函数，用于验证单个密钥并处理异常"""
        nonlocal successful_keys, failed_keys
        try:
            gemini_request = GeminiRequest(
                contents=[GeminiContent(role="user", parts=[{"text": "hi"}])],
                generation_config={"temperature": 0.7, "topP": 1.0, "maxOutputTokens": 10}
            )
            await chat_service.generate_content(
                settings.TEST_MODEL,
                gemini_request,
                api_key
            )
            successful_keys.append(api_key)
            # 如果密钥验证成功，则重置其失败计数
            await key_manager.reset_key_failure_count(api_key)
            return api_key, "valid", None
        except Exception as e:
            error_message = str(e)
            logger.warning(f"Key verification failed for {redact_key_for_logging(api_key)}: {error_message}")

            # 检查是否是429错误，如果是则调用专门的429错误处理机制
            is_429_error = "429" in error_message or "Too Many Requests" in error_message or "quota" in error_message.lower()

            if is_429_error and settings.ENABLE_KEY_FREEZE_ON_429:
                # 对于429错误，冷冻密钥而不是增加失败计数
                await key_manager.handle_429_error(api_key)
                logger.info(f"Bulk verification: Key {redact_key_for_logging(api_key)} frozen due to 429 error")
            else:
                # 对于其他错误，使用正常的失败处理逻辑
                async with key_manager.failure_count_lock:
                    if api_key in key_manager.key_failure_counts:
                        key_manager.key_failure_counts[api_key] += 1
                        logger.warning(f"Bulk verification exception for key: {redact_key_for_logging(api_key)}, incrementing failure count")
                    else:
                         key_manager.key_failure_counts[api_key] = 1
                         logger.warning(f"Bulk verification exception for key: {redact_key_for_logging(api_key)}, initializing failure count to 1")

            failed_keys[api_key] = error_message
            return api_key, "invalid", error_message

    tasks = [_verify_single_key(key) for key in keys_to_verify]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.error(f"An unexpected error occurred during bulk verification task: {result}")
        elif result:
             if not isinstance(result, Exception) and result:
                 key, status, error = result
             elif isinstance(result, Exception):
                 logger.error(f"Task execution error during bulk verification: {result}")

    valid_count = len(successful_keys)
    invalid_count = len(failed_keys)
    logger.info(f"Bulk verification finished. Valid: {valid_count}, Invalid: {invalid_count}")

    if failed_keys:
        message = f"批量验证完成。成功: {valid_count}, 失败: {invalid_count}。"
        return JSONResponse({
            "success": True,
            "message": message,
            "successful_keys": successful_keys,
            "failed_keys": failed_keys,
            "valid_count": valid_count,
            "invalid_count": invalid_count
        })
    else:
        message = f"批量验证成功完成。所有 {valid_count} 个密钥均有效。"
        return JSONResponse({
            "success": True,
            "message": message,
            "successful_keys": successful_keys,
            "failed_keys": {},
            "valid_count": valid_count,
            "invalid_count": 0
        })


@router.post("/batch-search-keys")
async def batch_search_keys(
    request: BatchSearchKeysRequest,
    key_manager: KeyManager = Depends(get_key_manager)
):
    """批量搜索密钥"""
    logger.info("-" * 50 + "batch_search_keys" + "-" * 50)

    try:
        # 解析输入的密钥
        keys_input = request.keys_input.strip()
        if not keys_input:
            return JSONResponse({"success": False, "message": "请输入要搜索的密钥"}, status_code=400)

        # 支持分号、半角逗号或换行分割
        if ';' in keys_input:
            search_keys = [key.strip() for key in keys_input.split(';') if key.strip()]
        elif ',' in keys_input:
            search_keys = [key.strip() for key in keys_input.split(',') if key.strip()]
        else:
            search_keys = [key.strip() for key in keys_input.split('\n') if key.strip()]

        if not search_keys:
            return JSONResponse({"success": False, "message": "未找到有效的密钥"}, status_code=400)

        # 获取所有密钥状态
        keys_status = await key_manager.get_keys_by_status()
        all_keys = {**keys_status["valid_keys"], **keys_status["invalid_keys"], **keys_status["disabled_keys"]}

        # 搜索匹配的密钥
        found_keys = {}
        not_found_keys = []

        for search_key in search_keys:
            if search_key in all_keys:
                key_info = all_keys[search_key]
                # 判断密钥状态
                if isinstance(key_info, dict):
                    fail_count = key_info.get("fail_count", 0)
                    disabled = key_info.get("disabled", False)
                    frozen = key_info.get("frozen", False)
                else:
                    # 兼容旧格式
                    fail_count = key_info
                    disabled = await key_manager.is_key_disabled(search_key)
                    frozen = await key_manager.is_key_frozen(search_key)

                status = "valid" if fail_count < key_manager.MAX_FAILURES and not disabled else "invalid"
                found_keys[search_key] = {
                    "status": status,
                    "fail_count": fail_count,
                    "disabled": disabled,
                    "frozen": frozen
                }
            else:
                not_found_keys.append(search_key)

        return JSONResponse({
            "success": True,
            "message": f"搜索完成，找到 {len(found_keys)} 个密钥",
            "found_keys": found_keys,
            "not_found_keys": not_found_keys,
            "search_count": len(search_keys),
            "found_count": len(found_keys)
        })
    except Exception as e:
        logger.error(f"Failed to search keys: {str(e)}")
        return JSONResponse({"success": False, "message": f"搜索失败: {str(e)}"}, status_code=500)


@router.get("/keys-paginated")
async def get_keys_paginated(
    key_type: str = "valid",
    page: int = 1,
    page_size: int = 10,
    search: str = None,
    fail_count_threshold: int = 0,
    key_manager: KeyManager = Depends(get_key_manager)
):
    """获取分页的密钥列表"""
    logger.info("-" * 50 + "get_keys_paginated" + "-" * 50)

    try:
        # 验证参数
        if key_type not in ["valid", "invalid", "disabled"]:
            return JSONResponse({"success": False, "message": "无效的密钥类型"}, status_code=400)

        if page < 1:
            page = 1
        if page_size < 1 or page_size > 1000:
            page_size = 10
        if fail_count_threshold < 0:
            fail_count_threshold = 0

        logger.info(f"Getting paginated keys: type={key_type}, page={page}, page_size={page_size}, search={search}, threshold={fail_count_threshold}")

        # 获取分页数据
        result = await key_manager.get_keys_by_status_paginated(
            key_type=key_type,
            page=page,
            page_size=page_size,
            search=search,
            fail_count_threshold=fail_count_threshold
        )

        return JSONResponse({
            "success": True,
            "data": result["keys"],
            "total_count": result["total_count"],
            "page": result["page"],
            "page_size": result["page_size"],
            "total_pages": result["total_pages"],
            "has_next": result["has_next"],
            "has_prev": result["has_prev"]
        })

    except Exception as e:
        logger.error(f"Failed to get paginated keys: {str(e)}")
        return JSONResponse({"success": False, "message": f"获取密钥列表失败: {str(e)}"}, status_code=500)


# 预检配置相关模型（简化版本）
class KeyPrecheckConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    count: Optional[int] = None
    trigger_ratio: Optional[float] = None


class KeyPrecheckConfigResponse(BaseModel):
    enabled: bool
    count: int
    trigger_ratio: float
    # 内部固定参数（不通过API暴露）
    min_keys_multiplier: int
    estimated_concurrent: int
    dynamic_adjustment: bool
    safety_buffer_ratio: float
    min_reserve_ratio: float
    # 状态信息
    min_keys_required: int
    current_keys_count: int
    last_minute_calls: int
    current_batch_size: int
    current_batch_valid_count: int
    valid_keys_passed_count: int
    valid_keys_trigger_threshold: int
    current_batch_valid_keys: List[int]
    next_batch_ready: bool
    next_batch_valid_count: int


@router.get("/precheck-config")
async def get_precheck_config(
    key_manager: KeyManager = Depends(get_key_manager)
):
    """获取密钥预检配置"""
    logger.info("-" * 50 + "get_precheck_config" + "-" * 50)

    try:
        # 确保兼容性字段是最新的
        key_manager._update_compatibility_fields()

        config = {
            "enabled": key_manager.precheck_enabled,
            "count": key_manager.precheck_count,
            "trigger_ratio": key_manager.precheck_trigger_ratio,
            # 状态信息（优先使用新字段，兼容旧字段）
            "current_keys_count": len(key_manager.api_keys),
            "last_minute_calls": key_manager.last_minute_calls,
            "current_batch_size": key_manager.precheck_count,  # 直接使用配置值
            "current_batch_valid_count": key_manager.current_batch_valid_count,  # 兼容性字段
            "valid_keys_passed_count": key_manager.valid_keys_used_count,  # 保持前端兼容的字段名
            "valid_keys_trigger_threshold": key_manager.valid_keys_trigger_threshold,
            "current_batch_valid_keys": key_manager.current_batch_valid_keys,  # 返回完整的有效密钥位置列表
            "current_key_position": key_manager.get_current_key_position(),  # 当前密钥指针位置
            "next_batch_ready": key_manager.next_batch_ready,  # 使用实际状态
            "next_batch_valid_count": key_manager.next_valid_count  # 使用实际数据
        }

        logger.info(f"Current precheck config: {config}")
        return JSONResponse({
            "success": True,
            "data": config
        })
    except Exception as e:
        logger.error(f"Failed to get precheck config: {str(e)}")
        return JSONResponse({"success": False, "message": f"获取预检配置失败: {str(e)}"}, status_code=500)


@router.post("/precheck-config")
async def update_precheck_config(
    request: KeyPrecheckConfigRequest,
    key_manager: KeyManager = Depends(get_key_manager)
):
    """更新密钥预检配置"""
    logger.info("-" * 50 + "update_precheck_config" + "-" * 50)

    try:
        # 验证参数（简化版本）
        if request.count is not None and (request.count < 10 or request.count > 1000):
            return JSONResponse({"success": False, "message": "预检数量必须在10-1000之间"}, status_code=400)

        if request.trigger_ratio is not None and (request.trigger_ratio < 0.1 or request.trigger_ratio > 1.0):
            return JSONResponse({"success": False, "message": "触发比例必须在0.1-1.0之间"}, status_code=400)

        # 更新配置（只更新核心参数）
        await key_manager.update_precheck_config(
            enabled=request.enabled,
            count=request.count,
            trigger_ratio=request.trigger_ratio
        )

        # 同时更新全局settings（简化版本，只更新核心参数）
        if request.enabled is not None:
            settings.KEY_PRECHECK_ENABLED = request.enabled
        if request.count is not None:
            settings.KEY_PRECHECK_COUNT = request.count
        if request.trigger_ratio is not None:
            settings.KEY_PRECHECK_TRIGGER_RATIO = request.trigger_ratio

        # 确保兼容性字段是最新的
        key_manager._update_compatibility_fields()

        # 返回更新后的配置
        updated_config = {
            "enabled": key_manager.precheck_enabled,
            "count": key_manager.precheck_count,
            "trigger_ratio": key_manager.precheck_trigger_ratio,
            # 状态信息（优先使用新字段，兼容旧字段）
            "current_keys_count": len(key_manager.api_keys),
            "last_minute_calls": key_manager.last_minute_calls,
            "current_batch_size": key_manager.precheck_count,  # 直接使用配置值
            "current_batch_valid_count": key_manager.current_batch_valid_count,  # 兼容性字段
            "valid_keys_passed_count": key_manager.valid_keys_used_count,  # 保持前端兼容的字段名
            "valid_keys_trigger_threshold": key_manager.valid_keys_trigger_threshold,
            "current_batch_valid_keys": key_manager.current_batch_valid_keys,  # 返回完整的有效密钥位置列表
            "current_key_position": key_manager.get_current_key_position(),  # 当前密钥指针位置
            "next_batch_ready": key_manager.next_batch_ready,  # 使用实际状态
            "next_batch_valid_count": key_manager.next_valid_count  # 使用实际数据
        }

        logger.info(f"Precheck config updated: {updated_config}")
        return JSONResponse({
            "success": True,
            "message": "预检配置更新成功",
            "data": updated_config
        })
    except Exception as e:
        logger.error(f"Failed to update precheck config: {str(e)}")
        return JSONResponse({"success": False, "message": f"更新预检配置失败: {str(e)}"}, status_code=500)


@router.post("/manual-precheck")
async def manual_trigger_precheck(
    key_manager: KeyManager = Depends(get_key_manager)
):
    """手动触发预检操作"""
    logger.info("-" * 50 + "manual_trigger_precheck" + "-" * 50)

    try:
        result = await key_manager.manual_trigger_precheck()

        if result["success"]:
            logger.info(f"Manual precheck completed successfully: {result['data']}")
            return JSONResponse({
                "success": True,
                "message": result["message"],
                "data": result["data"]
            })
        else:
            logger.warning(f"Manual precheck failed: {result['message']}")
            return JSONResponse({
                "success": False,
                "message": result["message"]
            }, status_code=400)

    except Exception as e:
        logger.error(f"Failed to trigger manual precheck: {str(e)}")
        return JSONResponse({
            "success": False,
            "message": f"手动预检失败: {str(e)}"
        }, status_code=500)


@router.post("/batch-operation-keys")
async def batch_operation_keys(
    request: BatchOperationKeysRequest,
    key_manager: KeyManager = Depends(get_key_manager)
):
    """批量启用/禁用密钥"""
    logger.info("-" * 50 + "batch_operation_keys" + "-" * 50)

    try:
        keys = request.keys
        operation = request.operation
        key_type = request.key_type or "gemini"

        if not keys:
            return JSONResponse({"success": False, "message": "请提供要操作的密钥"}, status_code=400)

        logger.info(f"Performing {operation} operation on {len(keys)} {key_type} keys")

        results = {}
        success_count = 0

        for key in keys:
            try:
                if key_type == "vertex":
                    if operation == "enable":
                        result = await key_manager.enable_vertex_key(key)
                    else:  # disable
                        result = await key_manager.disable_vertex_key(key)
                else:  # gemini
                    if operation == "enable":
                        result = await key_manager.enable_key(key)
                    else:  # disable
                        result = await key_manager.disable_key(key)

                results[key] = result
                if result:
                    success_count += 1
            except Exception as e:
                logger.error(f"Error {operation} key {key}: {str(e)}")
                results[key] = False

        operation_text = "启用" if operation == "enable" else "禁用"
        return JSONResponse({
            "success": True,
            "message": f"批量{operation_text}完成，成功处理 {success_count}/{len(keys)} 个密钥",
            "results": results,
            "success_count": success_count,
            "total_count": len(keys)
        })
    except Exception as e:
        logger.error(f"Failed to perform batch operation: {str(e)}")
        return JSONResponse({"success": False, "message": f"批量操作失败: {str(e)}"}, status_code=500)



@router.post("/unfreeze-key")
async def unfreeze_key(
    request: KeyFreezeRequest,
    key_manager: KeyManager = Depends(get_key_manager)
):
    """解冻指定密钥"""
    logger.info("-" * 50 + "unfreeze_key" + "-" * 50)

    try:
        key = request.key
        key_type = request.key_type or "gemini"

        logger.info(f"Unfreezing {key_type} key: {key}")

        if key_type == "vertex":
            result = await key_manager.unfreeze_vertex_key(key)
        else:  # gemini
            result = await key_manager.unfreeze_key(key)

        if result:
            return JSONResponse({
                "success": True,
                "message": "密钥已解冻"
            })
        else:
            return JSONResponse({"success": False, "message": "密钥未处于冷冻状态或解冻失败"}, status_code=400)
    except Exception as e:
        logger.error(f"Failed to unfreeze key: {str(e)}")
        return JSONResponse({"success": False, "message": f"解冻失败: {str(e)}"}, status_code=500)