
from functools import wraps
from typing import Callable, TypeVar

from app.config.config import settings
from app.log.logger import get_retry_logger
from app.utils.helpers import redact_key_for_logging

T = TypeVar("T")
logger = get_retry_logger()


class RetryHandler:
    """重试处理装饰器"""

    def __init__(self, key_arg: str = "api_key"):
        self.key_arg = key_arg

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(settings.MAX_RETRIES):
                retries = attempt + 1
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logger.warning(
                        f"API call failed with error: {str(e)}. Attempt {retries} of {settings.MAX_RETRIES}"
                    )

                    # 从函数参数中获取 key_manager
                    key_manager = kwargs.get("key_manager")
                    if key_manager:
                        old_key = kwargs.get(self.key_arg)

                        # 检查是否是429错误
                        error_str = str(e)
                        is_429_error = "429" in error_str or "Too Many Requests" in error_str or "quota" in error_str.lower()

                        if is_429_error and settings.ENABLE_KEY_FREEZE_ON_429:
                            # 对于429错误，冷冻密钥而不是增加失败计数
                            await key_manager.handle_429_error(old_key)
                            new_key = await key_manager.get_next_working_key()
                        else:
                            # 对于其他错误，使用正常的失败处理逻辑
                            new_key = await key_manager.handle_api_failure(old_key, retries)

                        if new_key:
                            kwargs[self.key_arg] = new_key
                            logger.info(f"Switched to new API key: {redact_key_for_logging(new_key)}")
                        else:
                            logger.error(f"No valid API key available after {retries} retries.")
                            break

            logger.error(
                f"All retry attempts failed, raising final exception: {str(last_exception)}"
            )
            raise last_exception

        return wrapper
