import asyncio
from itertools import cycle
from typing import Dict, Union, List, Optional
from datetime import datetime, timedelta
import threading
import time

from app.config.config import settings
from app.log.logger import get_key_manager_logger
from app.utils.helpers import redact_key_for_logging

logger = get_key_manager_logger()


class KeyManager:
    def __init__(self, api_keys: list, vertex_api_keys: list):
        self.api_keys = api_keys
        self.vertex_api_keys = vertex_api_keys
        self.key_cycle = cycle(api_keys)
        self.vertex_key_cycle = cycle(vertex_api_keys)
        self.key_cycle_lock = asyncio.Lock()
        self.vertex_key_cycle_lock = asyncio.Lock()
        self.failure_count_lock = asyncio.Lock()
        self.vertex_failure_count_lock = asyncio.Lock()
        self.key_failure_counts: Dict[str, int] = {key: 0 for key in api_keys}
        self.vertex_key_failure_counts: Dict[str, int] = {
            key: 0 for key in vertex_api_keys
        }
        self.MAX_FAILURES = settings.MAX_FAILURES
        self.paid_key = settings.PAID_KEY

        # 密钥状态管理
        self.disabled_keys: set = set()  # 禁用的密钥（保留兼容性）
        self.disabled_vertex_keys: set = set()  # 禁用的Vertex密钥（保留兼容性）
        self.frozen_keys: Dict[str, datetime] = {}  # 自动冻结的密钥及其解冻时间
        self.frozen_vertex_keys: Dict[str, datetime] = {}  # 自动冻结的Vertex密钥及其解冻时间
        self.manually_frozen_keys: set = set()  # 手动冻结的密钥（需要手动解冻）
        self.manually_frozen_vertex_keys: set = set()  # 手动冻结的Vertex密钥（需要手动解冻）
        self.key_state_lock = asyncio.Lock()  # 密钥状态锁
        self.vertex_key_state_lock = asyncio.Lock()  # Vertex密钥状态锁

        # 简化的预检机制（只保留核心参数）
        self.precheck_enabled = settings.KEY_PRECHECK_ENABLED
        self.precheck_count = settings.KEY_PRECHECK_COUNT
        self.precheck_trigger_ratio = settings.KEY_PRECHECK_TRIGGER_RATIO
        self.precheck_lock = asyncio.Lock()  # 预检锁

        # 简化的预检状态跟踪
        self.precheck_in_progress = False  # 是否正在进行预检
        self.precheck_last_position = 0  # 上次预检的结束位置
        self.key_usage_counter = 0  # 密钥使用计数器

        # 双缓冲有效密钥管理（按照用户期望重新设计）
        # 使用两个独立的存储空间（Map A 和 Map B）轮换使用
        self.valid_keys_batch_a = []  # 批次A：有效密钥列表（存储实际密钥）
        self.valid_keys_batch_b = []  # 批次B：有效密钥列表（存储实际密钥）

        # 当前使用的批次（'A' 或 'B'）
        self.current_batch_name = 'A'
        self.current_batch_index = 0  # 当前批次中的指针位置

        # 预检状态跟踪
        self.batch_a_ready = False  # 批次A是否准备就绪
        self.batch_b_ready = False  # 批次B是否准备就绪

        # 触发控制
        self.valid_keys_used_count = 0  # 已使用的有效密钥数量
        self.valid_keys_trigger_threshold = 0  # 触发阈值

        # 兼容性字段（用于API返回）
        self.current_batch_valid_keys = []  # 兼容旧API
        self.current_batch_valid_count = 0  # 兼容旧API

        # 保持兼容性的下一批次属性（简化版本）
        self.next_batch_valid_keys = []  # 下一批次有效密钥位置列表
        self.next_batch_valid_count = 0  # 下一批次有效密钥总数
        self.next_batch_ready = False  # 下一批次是否准备就绪
        self.next_valid_count = 0  # 兼容性字段：下一批次有效密钥数量
        self.precheck_base_position = 0  # 当前预检批次的起始位置

        # API调用统计（简化）
        self.last_minute_calls = 0  # 上一分钟的调用次数
        self.stats_update_time = datetime.now()  # 统计更新时间

        # 简化的启用条件：只要有密钥就启用
        if self.precheck_enabled and len(self.api_keys) > 0:
            logger.info(f"Simplified precheck enabled: count={self.precheck_count}, trigger_ratio={self.precheck_trigger_ratio}")
            # 执行初始预检
            asyncio.create_task(self._perform_initial_precheck())
        else:
            logger.info(f"Precheck disabled: enabled={self.precheck_enabled}, keys_count={len(self.api_keys)}")

    def _get_current_batch(self) -> List[str]:
        """获取当前使用的批次"""
        if self.current_batch_name == 'A':
            return self.valid_keys_batch_a
        else:
            return self.valid_keys_batch_b

    def _get_next_batch(self) -> List[str]:
        """获取下一个批次"""
        if self.current_batch_name == 'A':
            return self.valid_keys_batch_b
        else:
            return self.valid_keys_batch_a

    def _is_current_batch_ready(self) -> bool:
        """检查当前批次是否准备就绪"""
        if self.current_batch_name == 'A':
            return self.batch_a_ready
        else:
            return self.batch_b_ready

    def _is_next_batch_ready(self) -> bool:
        """检查下一批次是否准备就绪"""
        if self.current_batch_name == 'A':
            return self.batch_b_ready
        else:
            return self.batch_a_ready

    def _set_current_batch_ready(self, ready: bool):
        """设置当前批次准备状态"""
        if self.current_batch_name == 'A':
            self.batch_a_ready = ready
        else:
            self.batch_b_ready = ready

    def _set_next_batch_ready(self, ready: bool):
        """设置下一批次准备状态"""
        if self.current_batch_name == 'A':
            self.batch_b_ready = ready
        else:
            self.batch_a_ready = ready

    def _switch_to_next_batch(self):
        """切换到下一批次"""
        if self.current_batch_name == 'A':
            self.current_batch_name = 'B'
        else:
            self.current_batch_name = 'A'
        self.current_batch_index = 0
        self.valid_keys_used_count = 0

    async def get_paid_key(self) -> str:
        return self.paid_key

    async def get_next_key(self) -> str:
        """获取下一个API key"""
        async with self.key_cycle_lock:
            key = next(self.key_cycle)

            # 更新使用计数器（但不在这里触发预检检查）
            if self.precheck_enabled:
                self.key_usage_counter += 1

            return key

    def get_current_key_position(self) -> int:
        """获取当前密钥指针在api_keys列表中的位置"""
        if not self.api_keys:
            return 0
        # 基于key_usage_counter计算当前位置
        return (self.key_usage_counter - 1) % len(self.api_keys)

    async def get_next_vertex_key(self) -> str:
        """获取下一个 Vertex Express API key"""
        async with self.vertex_key_cycle_lock:
            return next(self.vertex_key_cycle)

    async def is_key_valid(self, key: str) -> bool:
        """检查key是否有效（考虑失败次数、禁用状态和冷冻状态）"""
        # 检查是否被禁用
        if await self.is_key_disabled(key):
            return False

        # 检查是否被冷冻
        if await self.is_key_frozen(key):
            return False

        # 检查失败次数
        async with self.failure_count_lock:
            return self.key_failure_counts[key] < self.MAX_FAILURES

    async def is_vertex_key_valid(self, key: str) -> bool:
        """检查 Vertex key 是否有效（考虑失败次数、禁用状态和冷冻状态）"""
        # 检查是否被禁用
        if await self.is_vertex_key_disabled(key):
            return False

        # 检查是否被冷冻
        if await self.is_vertex_key_frozen(key):
            return False

        # 检查失败次数
        async with self.vertex_failure_count_lock:
            return self.vertex_key_failure_counts[key] < self.MAX_FAILURES

    async def reset_failure_counts(self):
        """重置所有key的失败计数"""
        async with self.failure_count_lock:
            for key in self.key_failure_counts:
                self.key_failure_counts[key] = 0

    async def reset_vertex_failure_counts(self):
        """重置所有 Vertex key 的失败计数"""
        async with self.vertex_failure_count_lock:
            for key in self.vertex_key_failure_counts:
                self.vertex_key_failure_counts[key] = 0

    async def reset_key_failure_count(self, key: str) -> bool:
        """重置指定key的失败计数"""
        async with self.failure_count_lock:
            if key in self.key_failure_counts:
                self.key_failure_counts[key] = 0
                logger.info(f"Reset failure count for key: {redact_key_for_logging(key)}")
                return True
            logger.warning(
                f"Attempt to reset failure count for non-existent key: {key}"
            )
            return False

    async def reset_vertex_key_failure_count(self, key: str) -> bool:
        """重置指定 Vertex key 的失败计数"""
        async with self.vertex_failure_count_lock:
            if key in self.vertex_key_failure_counts:
                self.vertex_key_failure_counts[key] = 0
                logger.info(f"Reset failure count for Vertex key: {redact_key_for_logging(key)}")
                return True
            logger.warning(
                f"Attempt to reset failure count for non-existent Vertex key: {key}"
            )
            return False

    async def get_next_working_key(self) -> str:
        """获取下一可用的API key（按照用户期望的双缓冲机制）"""
        if not self.precheck_enabled:
            # 如果预检未启用，使用原来的逻辑
            return await self._get_next_working_key_legacy()

        # 获取当前批次
        current_batch = self._get_current_batch()

        # 如果当前批次为空或未准备就绪，立即触发预检建立初始批次
        if not current_batch or not self._is_current_batch_ready():
            logger.info("Current batch is empty or not ready, triggering immediate precheck")
            await self._perform_precheck_async()
            # 等待预检完成
            await self._wait_for_precheck_completion()
            current_batch = self._get_current_batch()

            # 如果预检后仍然没有有效密钥，回退到传统方式
            if not current_batch:
                logger.warning("No valid keys found after precheck, falling back to legacy method")
                return await self._get_next_working_key_legacy()

        # 从当前批次中获取下一个密钥
        current_key = current_batch[self.current_batch_index]

        # 移动指针到下一个位置
        self.current_batch_index += 1
        self.valid_keys_used_count += 1

        logger.info(f"Using valid key {self.current_batch_index}/{len(current_batch)} from batch {self.current_batch_name}, used_count: {self.valid_keys_used_count}/{self.valid_keys_trigger_threshold}")

        # 检查是否需要触发下一次预检（达到触发阈值）
        if (self.valid_keys_used_count >= self.valid_keys_trigger_threshold and
            not self.precheck_in_progress and not self._is_next_batch_ready()):
            logger.info(f"Trigger threshold reached ({self.valid_keys_used_count}/{self.valid_keys_trigger_threshold}), starting background precheck for next batch")
            asyncio.create_task(self._perform_precheck_async())

        # 检查是否需要切换到下一批次（当前批次用完）
        if self.current_batch_index >= len(current_batch):
            await self._switch_to_next_batch_new()

        return current_key

    async def _switch_to_next_batch_new(self):
        """切换到下一批次（新的双缓冲机制）"""
        if self._is_next_batch_ready():
            logger.info(f"Switching from batch {self.current_batch_name} to next batch")
            # 切换到下一批次
            self._switch_to_next_batch()
            # 标记新的当前批次为准备就绪，旧的批次为未准备
            self._set_current_batch_ready(True)
            # 清空旧批次并标记为未准备
            if self.current_batch_name == 'A':
                self.valid_keys_batch_b.clear()
                self.batch_b_ready = False
            else:
                self.valid_keys_batch_a.clear()
                self.batch_a_ready = False

            # 重新计算触发阈值
            self._calculate_precheck_trigger()
            logger.info(f"Batch switched successfully. New trigger threshold: {self.valid_keys_trigger_threshold}")
        else:
            logger.warning("No next batch available, triggering emergency precheck")
            # 重置当前批次指针到开头，继续使用当前批次
            self.current_batch_index = 0
            # 触发紧急预检
            asyncio.create_task(self._perform_precheck_async())

    async def _get_next_working_key_legacy(self) -> str:
        """传统的获取有效密钥方式（兜底方案）"""
        initial_key = await self.get_next_key()
        current_key = initial_key

        while True:
            if await self.is_key_valid(current_key):
                return current_key

            current_key = await self.get_next_key()
            if current_key == initial_key:
                return current_key

    async def _wait_for_precheck_completion(self, max_wait_seconds: int = 30):
        """等待预检完成"""
        wait_count = 0
        while self.precheck_in_progress and wait_count < max_wait_seconds:
            await asyncio.sleep(1)
            wait_count += 1

        if wait_count >= max_wait_seconds:
            logger.warning(f"Precheck did not complete within {max_wait_seconds} seconds")



    async def get_next_working_vertex_key(self) -> str:
        """获取下一可用的 Vertex Express API key"""
        initial_key = await self.get_next_vertex_key()
        current_key = initial_key

        while True:
            if await self.is_vertex_key_valid(current_key):
                return current_key

            current_key = await self.get_next_vertex_key()
            if current_key == initial_key:
                return current_key

    async def handle_api_failure(self, api_key: str, retries: int) -> str:
        """处理API调用失败"""
        async with self.failure_count_lock:
            self.key_failure_counts[api_key] += 1
            if self.key_failure_counts[api_key] >= self.MAX_FAILURES:
                logger.warning(
                    f"API key {redact_key_for_logging(api_key)} has failed {self.MAX_FAILURES} times"
                )
        if retries < settings.MAX_RETRIES:
            return await self.get_next_working_key()
        else:
            return ""

    async def handle_vertex_api_failure(self, api_key: str, retries: int) -> str:
        """处理 Vertex Express API 调用失败"""
        async with self.vertex_failure_count_lock:
            self.vertex_key_failure_counts[api_key] += 1
            if self.vertex_key_failure_counts[api_key] >= self.MAX_FAILURES:
                logger.warning(
                    f"Vertex Express API key {redact_key_for_logging(api_key)} has failed {self.MAX_FAILURES} times"
                )
        if retries < settings.MAX_RETRIES:
            return await self.get_next_working_vertex_key()
        else:
            return ""

    def get_fail_count(self, key: str) -> int:
        """获取指定密钥的失败次数"""
        return self.key_failure_counts.get(key, 0)

    def get_vertex_fail_count(self, key: str) -> int:
        """获取指定 Vertex 密钥的失败次数"""
        return self.vertex_key_failure_counts.get(key, 0)

    async def get_keys_by_status(self) -> dict:
        """获取分类后的API key列表，包括失败次数和状态信息"""
        valid_keys = {}
        invalid_keys = {}
        frozen_keys = {}

        async with self.failure_count_lock:
            for key in self.api_keys:
                fail_count = self.key_failure_counts[key]

                # 获取密钥状态信息
                is_disabled = await self.is_key_disabled(key)
                is_frozen = await self.is_key_frozen(key)
                is_manually_frozen = key in self.manually_frozen_keys
                freeze_until = self.frozen_keys.get(key)

                key_info = {
                    "fail_count": fail_count,
                    "disabled": is_disabled,  # 保留兼容性
                    "frozen": is_frozen,
                    "manually_frozen": is_manually_frozen,
                    "freeze_until": freeze_until.isoformat() if freeze_until else None
                }

                if is_frozen or is_disabled:
                    # 冻结的密钥（包括手动冻结和自动冻结）
                    frozen_keys[key] = key_info
                elif fail_count < self.MAX_FAILURES:
                    # 有效密钥（未冻结且失败次数未达到上限）
                    valid_keys[key] = key_info
                else:
                    # 无效密钥（失败次数达到上限但未被冻结）
                    invalid_keys[key] = key_info

        return {
            "valid_keys": valid_keys,
            "invalid_keys": invalid_keys,
            "disabled_keys": frozen_keys,  # 保留兼容性，实际是冻结列表
            "frozen_keys": frozen_keys  # 新的冻结列表
        }

    async def get_keys_by_status_paginated(
        self,
        key_type: str = "valid",
        page: int = 1,
        page_size: int = 10,
        search: str = None,
        fail_count_threshold: int = 0
    ) -> dict:
        """获取分页的API key列表（优化版本，避免处理所有密钥）"""

        # 优化：直接按类型处理密钥，避免先获取所有密钥状态
        keys_list = []

        if key_type == "valid":
            # 只处理有效密钥
            async with self.failure_count_lock:
                for key in self.api_keys:
                    fail_count = self.key_failure_counts[key]

                    # 快速检查：失败次数过多直接跳过
                    if fail_count >= self.MAX_FAILURES:
                        continue

                    # 检查是否被冻结或禁用（只对可能有效的密钥检查）
                    if await self.is_key_frozen(key) or await self.is_key_disabled(key):
                        continue

                    # 应用失败次数阈值过滤
                    if fail_count_threshold > 0 and fail_count < fail_count_threshold:
                        continue

                    # 应用搜索过滤
                    if search and search.lower() not in key.lower():
                        continue

                    keys_list.append({
                        "key": key,
                        "fail_count": fail_count,
                        "disabled": False,
                        "frozen": False
                    })

        elif key_type == "invalid":
            # 只处理无效密钥
            async with self.failure_count_lock:
                for key in self.api_keys:
                    fail_count = self.key_failure_counts[key]

                    # 只包含失败次数达到上限且未被冻结的密钥
                    if fail_count < self.MAX_FAILURES:
                        continue

                    if await self.is_key_frozen(key) or await self.is_key_disabled(key):
                        continue

                    # 应用搜索过滤
                    if search and search.lower() not in key.lower():
                        continue

                    keys_list.append({
                        "key": key,
                        "fail_count": fail_count,
                        "disabled": False,
                        "frozen": False
                    })

        elif key_type == "disabled" or key_type == "frozen":
            # 只处理冻结/禁用的密钥
            async with self.key_state_lock:
                # 检查手动冻结的密钥
                for key in self.manually_frozen_keys:
                    if search and search.lower() not in key.lower():
                        continue

                    async with self.failure_count_lock:
                        fail_count = self.key_failure_counts.get(key, 0)

                    keys_list.append({
                        "key": key,
                        "fail_count": fail_count,
                        "disabled": True,
                        "frozen": True
                    })

                # 检查自动冻结的密钥（清理过期的）
                current_time = datetime.now()
                expired_keys = []
                for key, freeze_until in self.frozen_keys.items():
                    if current_time >= freeze_until:
                        expired_keys.append(key)
                        continue

                    if search and search.lower() not in key.lower():
                        continue

                    async with self.failure_count_lock:
                        fail_count = self.key_failure_counts.get(key, 0)

                    keys_list.append({
                        "key": key,
                        "fail_count": fail_count,
                        "disabled": True,
                        "frozen": True
                    })

                # 清理过期的冻结密钥
                for key in expired_keys:
                    del self.frozen_keys[key]
                    logger.info(f"Key {redact_key_for_logging(key)} auto-unfrozen (freeze period expired)")
        else:
            raise ValueError(f"Invalid key_type: {key_type}")

        # 按密钥名称排序以保证一致性
        keys_list.sort(key=lambda x: x["key"])

        # 计算分页信息
        total_count = len(keys_list)
        total_pages = (total_count + page_size - 1) // page_size  # 向上取整

        # 确保页码有效
        page = max(1, min(page, total_pages if total_pages > 0 else 1))

        # 计算分页范围
        start_index = (page - 1) * page_size
        end_index = start_index + page_size

        # 获取当前页的数据
        page_keys = keys_list[start_index:end_index]

        # 转换为字典格式以保持兼容性
        paginated_keys = {}
        for item in page_keys:
            key = item["key"]
            paginated_keys[key] = {
                "fail_count": item["fail_count"],
                "disabled": item["disabled"],
                "frozen": item["frozen"]
            }

        return {
            "keys": paginated_keys,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }

    async def get_vertex_keys_by_status(self) -> dict:
        """获取分类后的 Vertex Express API key 列表，包括失败次数"""
        valid_keys = {}
        invalid_keys = {}

        async with self.vertex_failure_count_lock:
            for key in self.vertex_api_keys:
                fail_count = self.vertex_key_failure_counts[key]
                if fail_count < self.MAX_FAILURES:
                    valid_keys[key] = fail_count
                else:
                    invalid_keys[key] = fail_count
        return {"valid_keys": valid_keys, "invalid_keys": invalid_keys}

    # 密钥冷冻管理方法
    async def freeze_key(self, key: str, duration_seconds: Optional[int] = None) -> bool:
        """冷冻指定密钥"""
        if duration_seconds is None:
            duration_seconds = settings.KEY_FREEZE_DURATION_SECONDS

        freeze_until = datetime.now() + timedelta(seconds=duration_seconds)
        async with self.key_state_lock:
            self.frozen_keys[key] = freeze_until
            logger.info(f"Key {redact_key_for_logging(key)} frozen until {freeze_until} (duration: {duration_seconds}s)")
            logger.info(f"Current frozen keys count: {len(self.frozen_keys)}")
            return True

    async def freeze_vertex_key(self, key: str, duration_seconds: Optional[int] = None) -> bool:
        """冷冻指定Vertex密钥"""
        if duration_seconds is None:
            duration_seconds = settings.KEY_FREEZE_DURATION_SECONDS

        freeze_until = datetime.now() + timedelta(seconds=duration_seconds)
        async with self.vertex_key_state_lock:
            self.frozen_vertex_keys[key] = freeze_until
            logger.info(f"Vertex key {key} frozen until {freeze_until}")
            return True

    async def unfreeze_key(self, key: str) -> bool:
        """解冻指定密钥（包括自动冻结和手动冻结）"""
        async with self.key_state_lock:
            unfrozen = False
            if key in self.frozen_keys:
                del self.frozen_keys[key]
                unfrozen = True
            if key in self.manually_frozen_keys:
                self.manually_frozen_keys.remove(key)
                unfrozen = True
            if unfrozen:
                logger.info(f"Key {key} unfrozen")
                return True
            return False

    async def unfreeze_vertex_key(self, key: str) -> bool:
        """解冻指定Vertex密钥（包括自动冻结和手动冻结）"""
        async with self.vertex_key_state_lock:
            unfrozen = False
            if key in self.frozen_vertex_keys:
                del self.frozen_vertex_keys[key]
                unfrozen = True
            if key in self.manually_frozen_vertex_keys:
                self.manually_frozen_vertex_keys.remove(key)
                unfrozen = True
            if unfrozen:
                logger.info(f"Vertex key {key} unfrozen")
                return True
            return False

    async def is_key_frozen(self, key: str) -> bool:
        """检查密钥是否被冻结（包括自动冻结和手动冻结）"""
        async with self.key_state_lock:
            # 检查手动冻结
            if key in self.manually_frozen_keys:
                return True

            # 检查自动冻结
            if key not in self.frozen_keys:
                return False

            # 检查是否已过期，如果过期则自动解冻
            if datetime.now() >= self.frozen_keys[key]:
                del self.frozen_keys[key]
                logger.info(f"Key {key} auto-unfrozen (freeze period expired)")
                return False
            return True

    async def is_vertex_key_frozen(self, key: str) -> bool:
        """检查Vertex密钥是否被冻结（包括自动冻结和手动冻结）"""
        async with self.vertex_key_state_lock:
            # 检查手动冻结
            if key in self.manually_frozen_vertex_keys:
                return True

            # 检查自动冻结
            if key not in self.frozen_vertex_keys:
                return False

            # 检查是否已过期，如果过期则自动解冻
            if datetime.now() >= self.frozen_vertex_keys[key]:
                del self.frozen_vertex_keys[key]
                logger.info(f"Vertex key {key} auto-unfrozen (freeze period expired)")
                return False
            return True

    # 手动冻结管理方法
    async def manually_freeze_key(self, key: str) -> bool:
        """手动冻结指定密钥（需要手动解冻）"""
        async with self.key_state_lock:
            self.manually_frozen_keys.add(key)
            logger.info(f"Key {key} manually frozen")
            return True

    async def manually_freeze_vertex_key(self, key: str) -> bool:
        """手动冻结指定Vertex密钥（需要手动解冻）"""
        async with self.vertex_key_state_lock:
            self.manually_frozen_vertex_keys.add(key)
            logger.info(f"Vertex key {key} manually frozen")
            return True

    # 密钥禁用管理方法（保留兼容性，实际上映射到手动冻结）
    async def disable_key(self, key: str) -> bool:
        """禁用指定密钥（实际上是手动冻结）"""
        return await self.manually_freeze_key(key)

    async def disable_vertex_key(self, key: str) -> bool:
        """禁用指定Vertex密钥（实际上是手动冻结）"""
        return await self.manually_freeze_vertex_key(key)

    async def enable_key(self, key: str) -> bool:
        """启用指定密钥（实际上是解冻）"""
        return await self.unfreeze_key(key)

    async def enable_vertex_key(self, key: str) -> bool:
        """启用指定Vertex密钥（实际上是解冻）"""
        return await self.unfreeze_vertex_key(key)

    async def is_key_disabled(self, key: str) -> bool:
        """检查密钥是否被禁用（兼容性方法，实际检查是否被手动冻结）"""
        async with self.key_state_lock:
            return key in self.manually_frozen_keys or key in self.disabled_keys

    async def is_vertex_key_disabled(self, key: str) -> bool:
        """检查Vertex密钥是否被禁用（兼容性方法，实际检查是否被手动冻结）"""
        async with self.vertex_key_state_lock:
            return key in self.manually_frozen_vertex_keys or key in self.disabled_vertex_keys



    # 批量操作方法
    async def batch_disable_keys(self, keys: List[str]) -> Dict[str, bool]:
        """批量禁用密钥"""
        results = {}
        for key in keys:
            if key in self.api_keys:
                results[key] = await self.disable_key(key)
            else:
                results[key] = False
                logger.warning(f"Key {key} not found in api_keys")
        return results

    async def batch_enable_keys(self, keys: List[str]) -> Dict[str, bool]:
        """批量启用密钥"""
        results = {}
        for key in keys:
            if key in self.api_keys:
                results[key] = await self.enable_key(key)
            else:
                results[key] = False
                logger.warning(f"Key {key} not found in api_keys")
        return results

    async def batch_disable_vertex_keys(self, keys: List[str]) -> Dict[str, bool]:
        """批量禁用Vertex密钥"""
        results = {}
        for key in keys:
            if key in self.vertex_api_keys:
                results[key] = await self.disable_vertex_key(key)
            else:
                results[key] = False
                logger.warning(f"Vertex key {key} not found in vertex_api_keys")
        return results

    async def batch_enable_vertex_keys(self, keys: List[str]) -> Dict[str, bool]:
        """批量启用Vertex密钥"""
        results = {}
        for key in keys:
            if key in self.vertex_api_keys:
                results[key] = await self.enable_vertex_key(key)
            else:
                results[key] = False
                logger.warning(f"Vertex key {key} not found in vertex_api_keys")
        return results

    # 429错误特殊处理方法
    async def handle_429_error(self, api_key: str, is_vertex: bool = False) -> bool:
        """处理429错误，冷冻密钥而不是增加失败计数"""
        logger.info(f"handle_429_error called: key={redact_key_for_logging(api_key)}, is_vertex={is_vertex}, freeze_enabled={settings.ENABLE_KEY_FREEZE_ON_429}")

        if not settings.ENABLE_KEY_FREEZE_ON_429:
            logger.warning(f"Key freeze on 429 is disabled, not freezing key {redact_key_for_logging(api_key)}")
            return False

        if is_vertex:
            result = await self.freeze_vertex_key(api_key)
            logger.warning(f"Vertex key {redact_key_for_logging(api_key)} frozen due to 429 error, result: {result}")
        else:
            result = await self.freeze_key(api_key)
            logger.warning(f"Key {redact_key_for_logging(api_key)} frozen due to 429 error, result: {result}")
        return True

    async def get_first_valid_key(self) -> str:
        """获取第一个有效的API key"""
        async with self.failure_count_lock:
            for key in self.key_failure_counts:
                if self.key_failure_counts[key] < self.MAX_FAILURES:
                    return key
        if self.api_keys:
            return self.api_keys[0]
        if not self.api_keys:
            logger.warning("API key list is empty, cannot get first valid key.")
            return ""
        return self.api_keys[0]

    # 预检机制相关方法
    def _should_enable_precheck(self) -> bool:
        """简化的预检启用检查"""
        # 简化：只要有密钥就启用预检
        return len(self.api_keys) > 0

    async def _check_precheck_trigger_for_valid_key(self):
        """预检触发检查（已废弃，逻辑已移至get_next_working_key）"""
        # 此方法已被新的预检机制替代，保留用于兼容性
        # 实际的触发逻辑现在在get_next_working_key中处理
        pass

    async def _update_api_call_stats(self):
        """更新API调用统计"""
        try:
            # 每分钟更新一次统计
            now = datetime.now()
            if (now - self.stats_update_time).total_seconds() >= 60:
                from app.service.stats.stats_service import StatsService
                stats_service = StatsService()
                stats = await stats_service.get_calls_in_last_minutes(1)
                self.last_minute_calls = stats.get('total', 0)
                self.stats_update_time = now

                # 简化：移除动态调整逻辑
                # 不再进行复杂的动态调整

                logger.debug(f"Updated API call stats: {self.last_minute_calls} calls in last minute")
        except Exception as e:
            logger.error(f"Failed to update API call stats: {e}")

    async def _adjust_precheck_parameters(self):
        """简化的参数调整（移除复杂的动态调整逻辑）"""
        # 简化：不再进行复杂的动态调整，保持配置的预检数量
        pass

    def _calculate_precheck_trigger(self):
        """计算预检触发阈值（按照用户期望的逻辑）"""
        if not self.precheck_enabled:
            self.valid_keys_trigger_threshold = 0
            logger.debug("Precheck disabled, trigger threshold set to 0")
            return

        # 使用新的双缓冲数据结构
        current_batch = self._get_current_batch()
        valid_count = len(current_batch)

        if valid_count == 0:
            self.valid_keys_trigger_threshold = 0
            logger.debug("No valid keys in current batch, trigger threshold set to 0")
            return

        # 按照用户期望：基于有效密钥数量和触发比例计算
        # 例如：12个有效密钥 × 0.5 = 6个（使用6个后触发下一次预检）
        self.valid_keys_trigger_threshold = max(1, int(valid_count * self.precheck_trigger_ratio))

        logger.info(f"Trigger threshold calculated: {self.valid_keys_trigger_threshold} (valid_keys={valid_count}, ratio={self.precheck_trigger_ratio})")

    async def _check_precheck_safety(self) -> bool:
        """检查预检安全性（确保剩余有效密钥足够）"""
        if not self.precheck_enabled:
            return True

        # 使用新的双缓冲机制计算剩余的有效密钥数量
        current_batch = self._get_current_batch()
        current_batch_count = len(current_batch)
        remaining_valid_keys = current_batch_count - self.valid_keys_used_count

        # 检查剩余有效密钥数量是否足够应对当前的调用频率
        if self.last_minute_calls > 0:
            # 预估需要的有效密钥数量（考虑1分钟的调用量）
            estimated_needed = min(self.last_minute_calls, current_batch_count)

            if remaining_valid_keys < estimated_needed:
                logger.warning(f"Precheck safety check failed: remaining_valid_keys={remaining_valid_keys}, needed={estimated_needed}")
                return False

        return True

    async def _perform_initial_precheck(self):
        """初始预检（使用双缓冲机制）"""
        try:
            logger.info("Starting initial precheck with dual buffer mechanism...")

            # 确保当前批次为空，这样预检结果会建立初始批次
            current_batch = self._get_current_batch()
            if current_batch:
                logger.info(f"Current batch {self.current_batch_name} already has {len(current_batch)} keys, skipping initial precheck")
                return

            # 执行预检建立初始批次
            await self._perform_precheck_async()

            # 检查结果
            current_batch_after = self._get_current_batch()
            logger.info(f"Initial precheck completed. Batch {self.current_batch_name}: {len(current_batch_after)} valid keys, trigger threshold: {self.valid_keys_trigger_threshold}")

            if len(current_batch_after) > 0:
                logger.info(f"Initial batch {self.current_batch_name} established successfully with {len(current_batch_after)} valid keys")
            else:
                logger.warning("Initial precheck failed to find any valid keys")

        except Exception as e:
            logger.error(f"Error in initial precheck: {e}", exc_info=True)

    async def _perform_precheck_async(self):
        """简化的异步预检执行"""
        async with self.precheck_lock:
            if self.precheck_in_progress:
                return

            self.precheck_in_progress = True

        try:
            # 简化：直接执行预检，移除复杂的安全检查和统计更新
            await self._perform_precheck()
        finally:
            async with self.precheck_lock:
                self.precheck_in_progress = False

    async def _perform_precheck(self):
        """按照用户期望重新实现的预检执行"""
        if not self.precheck_enabled or not self.api_keys:
            return

        try:
            # 使用配置的预检数量
            batch_size = self.precheck_count

            # 计算预检起始位置（从当前密钥指针位置开始）
            current_position = self.get_current_key_position()
            start_index = current_position
            keys_to_check = self._get_precheck_keys(start_index, batch_size)

            logger.info(f"Precheck starting from current key position: {current_position} (key_usage_counter: {self.key_usage_counter})")

            if not keys_to_check:
                logger.warning("No keys to check in precheck")
                return

            logger.info(f"Starting precheck: start_index={start_index}, checking {len(keys_to_check)} keys")

            # 并发检查这些密钥
            tasks = []
            for key in keys_to_check:
                task = asyncio.create_task(self._precheck_single_key(key))
                tasks.append(task)

            # 等待所有检查完成
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 统计结果并收集有效密钥
            valid_keys = []
            invalid_count = 0
            error_count = 0

            for i, result in enumerate(results):
                if result is True:
                    valid_keys.append(keys_to_check[i])
                elif result is False:
                    invalid_count += 1
                elif isinstance(result, Exception):
                    error_count += 1
                    logger.error(f"Precheck task {i} failed: {result}")

            logger.info(f"Precheck completed: {len(valid_keys)} valid, {invalid_count} invalid, {error_count} errors")
            logger.info(f"Valid keys found: {[key[:20] + '...' for key in valid_keys]}")

            # 更新预检位置（重要：确保下次从正确位置开始）
            self.precheck_last_position = (start_index + batch_size) % len(self.api_keys)
            logger.info(f"Next precheck will start from position: {self.precheck_last_position}")

            # 使用新的双缓冲机制处理预检结果
            current_batch = self._get_current_batch()
            logger.info(f"Current batch status: batch_{self.current_batch_name}={len(current_batch)}, next_batch_ready={self._is_next_batch_ready()}")
            logger.info(f"Precheck found {len(valid_keys)} valid keys out of {len(keys_to_check)} checked")

            # 检查是否找到了有效密钥
            if len(valid_keys) == 0:
                logger.warning("Precheck found no valid keys - all checked keys are invalid")
                # 如果当前批次也为空，这是一个严重问题
                if not current_batch or not self._is_current_batch_ready():
                    logger.error("No valid keys found and no current batch available - system may have no working keys")
                return

            if not current_batch or not self._is_current_batch_ready():
                # 初始预检或当前批次为空，直接建立新批次
                logger.info(f"Establishing initial batch {self.current_batch_name}: {len(valid_keys)} valid keys")
                self._establish_new_batch_dual_buffer(valid_keys)
            elif self._is_next_batch_ready():
                # 下一批次已经准备好，这次预检结果将被丢弃
                logger.info(f"Next batch already ready, current precheck result will be discarded")
            else:
                # 将预检结果作为下一批次
                logger.info(f"Queuing next batch: {len(valid_keys)} valid keys")
                self._queue_next_batch_dual_buffer(valid_keys)

        except Exception as e:
            logger.error(f"Error in precheck operation: {e}")



    def _establish_new_batch_dual_buffer(self, valid_keys: list):
        """建立新的有效密钥批次（双缓冲机制）"""
        logger.info(f"Establishing new batch {self.current_batch_name} with {len(valid_keys)} valid keys")

        # 将有效密钥存储到当前批次
        if self.current_batch_name == 'A':
            self.valid_keys_batch_a = valid_keys.copy()
        else:
            self.valid_keys_batch_b = valid_keys.copy()

        # 重置指针和计数器
        self.current_batch_index = 0
        self.valid_keys_used_count = 0

        # 标记当前批次为准备就绪
        self._set_current_batch_ready(True)

        # 重新计算触发阈值
        self._calculate_precheck_trigger()

        # 更新兼容性字段
        self._update_compatibility_fields()

        logger.info(f"New batch {self.current_batch_name} established: {len(valid_keys)} valid keys, trigger threshold: {self.valid_keys_trigger_threshold}")

    def _queue_next_batch_dual_buffer(self, valid_keys: list):
        """将有效密钥加入下一批次队列（双缓冲机制）"""
        # 将有效密钥存储到下一批次
        if self.current_batch_name == 'A':
            self.valid_keys_batch_b = valid_keys.copy()
        else:
            self.valid_keys_batch_a = valid_keys.copy()

        # 标记下一批次为准备就绪
        self._set_next_batch_ready(True)

        logger.info(f"Next batch queued: {len(valid_keys)} valid keys ready for switch")

    def _update_compatibility_fields(self):
        """更新兼容性字段（用于API返回）"""
        # 获取当前批次
        current_batch = self._get_current_batch()
        next_batch = self._get_next_batch()

        # 将实际密钥转换为位置索引（用于兼容旧API）
        self.current_batch_valid_keys = []
        for key in current_batch:
            try:
                position = self.api_keys.index(key)
                self.current_batch_valid_keys.append(position)
            except ValueError:
                logger.warning(f"Valid key not found in api_keys list: {key[:20]}...")

        self.current_batch_valid_count = len(current_batch)

        # 更新下一批次的兼容性字段
        self.next_batch_valid_keys = []
        for key in next_batch:
            try:
                position = self.api_keys.index(key)
                self.next_batch_valid_keys.append(position)
            except ValueError:
                logger.warning(f"Next batch key not found in api_keys list: {key[:20]}...")

        self.next_batch_valid_count = len(next_batch)
        self.next_batch_ready = self._is_next_batch_ready()
        self.next_valid_count = len(next_batch)  # 兼容性字段

    async def _precheck_single_key(self, key: str) -> bool:
        """预检单个密钥（改进版本）"""
        try:
            # 对于预检，我们要更宽松一些，即使失败次数较高也要尝试验证
            # 只跳过明确被冻结或禁用的密钥
            if await self.is_key_frozen(key) or await self.is_key_disabled(key):
                logger.debug(f"Key {redact_key_for_logging(key)} is frozen/disabled, skipping precheck")
                return False

            # 执行实际的API验证
            is_valid = await self._validate_key_with_api(key)

            if not is_valid:
                logger.debug(f"Precheck detected invalid key: {redact_key_for_logging(key)}")
                return False
            else:
                logger.debug(f"Precheck confirmed key validity: {redact_key_for_logging(key)}")
                return True

        except Exception as e:
            logger.error(f"Error in precheck single key {redact_key_for_logging(key)}: {e}")
            return False



    def _get_precheck_keys(self, start_index: int, count: int) -> List[str]:
        """获取预检密钥列表（优先选择可能有效的密钥）"""
        if not self.api_keys or count <= 0:
            return []

        # 首先尝试获取可能有效的密钥（失败次数较少的）
        potentially_valid_keys = []
        for key in self.api_keys:
            fail_count = self.key_failure_counts.get(key, 0)
            if fail_count < self.MAX_FAILURES:
                potentially_valid_keys.append(key)

        # 如果有足够的可能有效密钥，优先使用它们
        if len(potentially_valid_keys) >= count:
            # 从start_index开始循环选择
            keys = []
            for i in range(count):
                index = (start_index + i) % len(potentially_valid_keys)
                keys.append(potentially_valid_keys[index])
            logger.info(f"Precheck selecting {len(keys)} potentially valid keys (fail_count < {self.MAX_FAILURES})")
            return keys

        # 如果可能有效的密钥不够，则包含所有密钥但优先选择失败次数少的
        all_keys_with_priority = sorted(self.api_keys, key=lambda k: self.key_failure_counts.get(k, 0))

        keys = []
        for i in range(min(count, len(all_keys_with_priority))):
            index = (start_index + i) % len(all_keys_with_priority)
            keys.append(all_keys_with_priority[index])

        logger.info(f"Precheck selecting {len(keys)} keys (including some with higher fail counts)")
        return keys



    # 移除重复的方法定义，使用上面的实现

    async def _precheck_single_key_with_position(self, key: str, position: int):
        """预检单个密钥并返回是否有效"""
        try:
            # 检查密钥是否已经无效、禁用或冷冻
            if not await self.is_key_valid(key):
                logger.debug(f"Key {redact_key_for_logging(key)} at position {position} already invalid, skipping precheck")
                return False

            # 执行实际的API验证
            is_valid = await self._validate_key_with_api(key)

            if not is_valid:
                logger.warning(f"Precheck detected invalid key: {redact_key_for_logging(key)} at position {position}")
                # _validate_key_with_api 已经处理了429错误的冻结和400等错误的标记无效
                # 这里不需要再次处理
                return False
            else:
                logger.debug(f"Precheck confirmed key validity: {redact_key_for_logging(key)} at position {position}")
                return True

        except Exception as e:
            logger.error(f"Error prechecking key {redact_key_for_logging(key)} at position {position}: {e}")
            return None  # 返回None表示检查出错

    async def _validate_key_with_api(self, key: str) -> bool:
        """使用API验证密钥有效性（完全复制批量验证的逻辑）"""
        try:
            # 完全复制批量验证中的验证逻辑
            from app.service.chat.gemini_chat_service import GeminiChatService
            from app.model.gemini_request import GeminiRequest, GeminiContent

            # 获取聊天服务实例（与批量验证相同）
            chat_service = GeminiChatService()

            # 构造与批量验证完全相同的测试请求
            gemini_request = GeminiRequest(
                contents=[GeminiContent(role="user", parts=[{"text": "hi"}])],
                generation_config={"temperature": 0.7, "topP": 1.0, "maxOutputTokens": 10}
            )

            logger.debug(f"Precheck: Validating key {redact_key_for_logging(key)} using batch verification logic")

            # 执行与批量验证完全相同的API调用
            await chat_service.generate_content(
                settings.TEST_MODEL,
                gemini_request,
                key
            )

            # 验证成功 - 与批量验证相同的处理
            logger.debug(f"Precheck: Key {redact_key_for_logging(key)} is valid")
            # 如果密钥验证成功，则重置其失败计数（与批量验证保持一致）
            await self.reset_key_failure_count(key)
            return True

        except Exception as e:
            error_message = str(e)
            logger.debug(f"Precheck: Key {redact_key_for_logging(key)} validation failed: {error_message}")

            # 完全复制批量验证的错误处理逻辑
            is_429_error = "429" in error_message or "Too Many Requests" in error_message or "quota" in error_message.lower()

            if is_429_error and settings.ENABLE_KEY_FREEZE_ON_429:
                # 对于429错误，冷冻密钥而不是增加失败计数（与批量验证相同）
                await self.handle_429_error(key)
                logger.info(f"Precheck: Key {redact_key_for_logging(key)} frozen due to 429 error")
            else:
                # 对于其他错误，使用正常的失败处理逻辑（与批量验证相同）
                async with self.failure_count_lock:
                    if key in self.key_failure_counts:
                        self.key_failure_counts[key] += 1
                        logger.debug(f"Precheck: Key {redact_key_for_logging(key)}, incrementing failure count")
                    else:
                        self.key_failure_counts[key] = 1
                        logger.debug(f"Precheck: Key {redact_key_for_logging(key)}, initializing failure count to 1")

            return False

    async def update_precheck_config(self, enabled: bool = None, count: int = None, trigger_ratio: float = None):
        """更新预检配置（简化版本，只支持核心参数）"""
        from app.config.config import settings
        from app.service.config.config_service import ConfigService

        config_changed = False
        config_updates = {}

        if enabled is not None and enabled != self.precheck_enabled:
            self.precheck_enabled = enabled
            settings.KEY_PRECHECK_ENABLED = enabled
            config_updates["KEY_PRECHECK_ENABLED"] = enabled
            config_changed = True

        if count is not None and count != self.precheck_count:
            self.precheck_count = max(10, count)  # 最小为10
            settings.KEY_PRECHECK_COUNT = self.precheck_count
            config_updates["KEY_PRECHECK_COUNT"] = self.precheck_count
            config_changed = True

        if trigger_ratio is not None and trigger_ratio != self.precheck_trigger_ratio:
            self.precheck_trigger_ratio = max(0.1, min(1.0, trigger_ratio))  # 限制在0.1-1.0之间
            settings.KEY_PRECHECK_TRIGGER_RATIO = self.precheck_trigger_ratio
            config_updates["KEY_PRECHECK_TRIGGER_RATIO"] = self.precheck_trigger_ratio
            config_changed = True

        if config_changed:
            logger.info(f"Simplified precheck config updated: enabled={self.precheck_enabled}, count={self.precheck_count}, trigger_ratio={self.precheck_trigger_ratio}")

            # 保存配置到数据库
            try:
                await ConfigService.update_config(config_updates)
                logger.info(f"Precheck config saved to database: {config_updates}")
            except Exception as e:
                logger.error(f"Failed to save precheck config to database: {str(e)}")

            # 重新检查是否应该启用预检
            if self.precheck_enabled and not self._should_enable_precheck():
                self.precheck_enabled = False
                settings.KEY_PRECHECK_ENABLED = False
                # 更新数据库中的启用状态
                try:
                    await ConfigService.update_config({"KEY_PRECHECK_ENABLED": False})
                except Exception as e:
                    logger.error(f"Failed to update precheck enabled status in database: {str(e)}")
                logger.warning("Precheck disabled due to insufficient keys")
            elif self.precheck_enabled:
                # 重新计算触发点
                self._calculate_precheck_trigger()
                # 如果当前没有在进行预检，立即执行一次
                if not self.precheck_in_progress:
                    asyncio.create_task(self._perform_precheck_async())

    async def manual_trigger_precheck(self) -> dict:
        """手动触发预检操作"""
        if not self.precheck_enabled:
            return {
                "success": False,
                "message": "预检机制未启用",
                "data": None
            }

        if self.precheck_in_progress:
            return {
                "success": False,
                "message": "预检正在进行中，请稍后再试",
                "data": None
            }

        try:
            import time
            start_time = time.time()

            logger.info("Manual precheck triggered by user")

            # 确保兼容性字段是最新的
            self._update_compatibility_fields()

            # 记录触发前的状态（使用新的双缓冲数据结构）
            current_batch = self._get_current_batch()
            before_state = {
                "current_batch_name": self.current_batch_name,
                "current_batch_valid_count": len(current_batch),
                "valid_keys_passed_count": self.valid_keys_used_count,
                "trigger_threshold": self.valid_keys_trigger_threshold,
                "current_batch_ready": self._is_current_batch_ready(),
                "next_batch_ready": self._is_next_batch_ready()
            }

            logger.info(f"Before precheck: batch_{self.current_batch_name}={len(current_batch)}, used_count={self.valid_keys_used_count}, current_ready={self._is_current_batch_ready()}, next_ready={self._is_next_batch_ready()}")

            # 检查是否有可用的密钥进行预检
            available_keys = 0
            async with self.failure_count_lock:
                for key in self.api_keys:
                    if self.key_failure_counts.get(key, 0) < self.MAX_FAILURES:
                        available_keys += 1

            logger.info(f"Available keys for precheck: {available_keys}/{len(self.api_keys)}")

            if available_keys == 0:
                logger.warning("No available keys for precheck - all keys may have reached MAX_FAILURES")
                # 重置一些密钥的失败计数以允许重试
                async with self.failure_count_lock:
                    reset_count = 0
                    for key in list(self.key_failure_counts.keys())[:5]:  # 重置前5个密钥
                        self.key_failure_counts[key] = 0
                        reset_count += 1
                    logger.info(f"Reset failure count for {reset_count} keys to allow precheck retry")

            # 执行预检
            await self._perform_precheck_async()

            # 等待预检完成
            max_wait = 30  # 最多等待30秒
            wait_count = 0
            while self.precheck_in_progress and wait_count < max_wait:
                await asyncio.sleep(1)
                wait_count += 1

            # 更新兼容性字段
            self._update_compatibility_fields()

            # 记录触发后的状态（使用新的双缓冲数据结构）
            current_batch_after = self._get_current_batch()
            after_state = {
                "current_batch_name": self.current_batch_name,
                "current_batch_valid_count": len(current_batch_after),
                "valid_keys_passed_count": self.valid_keys_used_count,
                "trigger_threshold": self.valid_keys_trigger_threshold,
                "current_batch_ready": self._is_current_batch_ready(),
                "next_batch_ready": self._is_next_batch_ready()
            }

            execution_time = round(time.time() - start_time, 2)
            logger.info(f"After precheck: batch_{self.current_batch_name}={len(current_batch_after)}, used_count={self.valid_keys_used_count}, execution_time={execution_time}s")

            # 详细的结果分析
            if len(current_batch_after) > 0:
                logger.info(f"Manual precheck SUCCESS: Found {len(current_batch_after)} valid keys in batch {self.current_batch_name}")
            else:
                logger.warning(f"Manual precheck FAILED: No valid keys found")
                # 简化的失败原因诊断（线程安全）
                async with self.key_state_lock:
                    frozen_count = len(self.frozen_keys) + len(self.manually_frozen_keys)
                high_failure_keys = 0
                async with self.failure_count_lock:
                    for key, count in self.key_failure_counts.items():
                        if count >= self.MAX_FAILURES:
                            high_failure_keys += 1
                logger.warning(f"Key status: frozen={frozen_count}, high_failure={high_failure_keys}, total={len(self.api_keys)}")

            logger.info(f"Manual precheck completed. Before: {before_state}, After: {after_state}")

            return {
                "success": True,
                "message": "预检执行成功",
                "data": {
                    "before": before_state,
                    "after": after_state,
                    "execution_time": execution_time
                }
            }

        except Exception as e:
            logger.error(f"Manual precheck failed: {str(e)}")
            return {
                "success": False,
                "message": f"预检执行失败: {str(e)}",
                "data": None
            }


_singleton_instance = None
_singleton_lock = asyncio.Lock()
_preserved_failure_counts: Union[Dict[str, int], None] = None
_preserved_vertex_failure_counts: Union[Dict[str, int], None] = None
_preserved_old_api_keys_for_reset: Union[list, None] = None
_preserved_vertex_old_api_keys_for_reset: Union[list, None] = None
_preserved_next_key_in_cycle: Union[str, None] = None
_preserved_vertex_next_key_in_cycle: Union[str, None] = None


async def get_key_manager_instance(
    api_keys: Optional[list] = None, vertex_api_keys: Optional[list] = None
) -> KeyManager:
    """
    获取 KeyManager 单例实例。

    如果尚未创建实例，将使用提供的 api_keys,vertex_api_keys 初始化 KeyManager。
    如果已创建实例，则忽略 api_keys 参数，返回现有单例。
    如果在重置后调用，会尝试恢复之前的状态（失败计数、循环位置）。
    """
    global _singleton_instance, _preserved_failure_counts, _preserved_vertex_failure_counts, _preserved_old_api_keys_for_reset, _preserved_vertex_old_api_keys_for_reset, _preserved_next_key_in_cycle, _preserved_vertex_next_key_in_cycle

    async with _singleton_lock:
        if _singleton_instance is None:
            if api_keys is None:
                raise ValueError(
                    "API keys are required to initialize or re-initialize the KeyManager instance."
                )
            if vertex_api_keys is None:
                raise ValueError(
                    "Vertex Express API keys are required to initialize or re-initialize the KeyManager instance."
                )

            if not api_keys:
                logger.warning(
                    "Initializing KeyManager with an empty list of API keys."
                )
            if not vertex_api_keys:
                logger.warning(
                    "Initializing KeyManager with an empty list of Vertex Express API keys."
                )

            _singleton_instance = KeyManager(api_keys, vertex_api_keys)
            logger.info(
                f"KeyManager instance created/re-created with {len(api_keys)} API keys and {len(vertex_api_keys)} Vertex Express API keys."
            )

            # 1. 恢复失败计数
            if _preserved_failure_counts:
                current_failure_counts = {
                    key: 0 for key in _singleton_instance.api_keys
                }
                for key, count in _preserved_failure_counts.items():
                    if key in current_failure_counts:
                        current_failure_counts[key] = count
                _singleton_instance.key_failure_counts = current_failure_counts
                logger.info("Inherited failure counts for applicable keys.")
            _preserved_failure_counts = None

            if _preserved_vertex_failure_counts:
                current_vertex_failure_counts = {
                    key: 0 for key in _singleton_instance.vertex_api_keys
                }
                for key, count in _preserved_vertex_failure_counts.items():
                    if key in current_vertex_failure_counts:
                        current_vertex_failure_counts[key] = count
                _singleton_instance.vertex_key_failure_counts = (
                    current_vertex_failure_counts
                )
                logger.info("Inherited failure counts for applicable Vertex keys.")
            _preserved_vertex_failure_counts = None

            # 2. 调整 key_cycle 的起始点
            start_key_for_new_cycle = None
            if (
                _preserved_old_api_keys_for_reset
                and _preserved_next_key_in_cycle
                and _singleton_instance.api_keys
            ):
                try:
                    start_idx_in_old = _preserved_old_api_keys_for_reset.index(
                        _preserved_next_key_in_cycle
                    )

                    for i in range(len(_preserved_old_api_keys_for_reset)):
                        current_old_key_idx = (start_idx_in_old + i) % len(
                            _preserved_old_api_keys_for_reset
                        )
                        key_candidate = _preserved_old_api_keys_for_reset[
                            current_old_key_idx
                        ]
                        if key_candidate in _singleton_instance.api_keys:
                            start_key_for_new_cycle = key_candidate
                            break
                except ValueError:
                    logger.warning(
                        f"Preserved next key '{_preserved_next_key_in_cycle}' not found in preserved old API keys. "
                        "New cycle will start from the beginning of the new list."
                    )
                except Exception as e:
                    logger.error(
                        f"Error determining start key for new cycle from preserved state: {e}. "
                        "New cycle will start from the beginning."
                    )

            if start_key_for_new_cycle and _singleton_instance.api_keys:
                try:
                    target_idx = _singleton_instance.api_keys.index(
                        start_key_for_new_cycle
                    )
                    for _ in range(target_idx):
                        next(_singleton_instance.key_cycle)
                    logger.info(
                        f"Key cycle in new instance advanced. Next call to get_next_key() will yield: {start_key_for_new_cycle}"
                    )
                except ValueError:
                    logger.warning(
                        f"Determined start key '{start_key_for_new_cycle}' not found in new API keys during cycle advancement. "
                        "New cycle will start from the beginning."
                    )
                except StopIteration:
                    logger.error(
                        "StopIteration while advancing key cycle, implies empty new API key list previously missed."
                    )
                except Exception as e:
                    logger.error(
                        f"Error advancing new key cycle: {e}. Cycle will start from beginning."
                    )
            else:
                if _singleton_instance.api_keys:
                    logger.info(
                        "New key cycle will start from the beginning of the new API key list (no specific start key determined or needed)."
                    )
                else:
                    logger.info(
                        "New key cycle not applicable as the new API key list is empty."
                    )

            # 清理所有保存的状态
            _preserved_old_api_keys_for_reset = None
            _preserved_next_key_in_cycle = None

            # 3. 调整 vertex_key_cycle 的起始点
            start_key_for_new_vertex_cycle = None
            if (
                _preserved_vertex_old_api_keys_for_reset
                and _preserved_vertex_next_key_in_cycle
                and _singleton_instance.vertex_api_keys
            ):
                try:
                    start_idx_in_old = _preserved_vertex_old_api_keys_for_reset.index(
                        _preserved_vertex_next_key_in_cycle
                    )

                    for i in range(len(_preserved_vertex_old_api_keys_for_reset)):
                        current_old_key_idx = (start_idx_in_old + i) % len(
                            _preserved_vertex_old_api_keys_for_reset
                        )
                        key_candidate = _preserved_vertex_old_api_keys_for_reset[
                            current_old_key_idx
                        ]
                        if key_candidate in _singleton_instance.vertex_api_keys:
                            start_key_for_new_vertex_cycle = key_candidate
                            break
                except ValueError:
                    logger.warning(
                        f"Preserved next key '{_preserved_vertex_next_key_in_cycle}' not found in preserved old Vertex Express API keys. "
                        "New cycle will start from the beginning of the new list."
                    )
                except Exception as e:
                    logger.error(
                        f"Error determining start key for new Vertex key cycle from preserved state: {e}. "
                        "New cycle will start from the beginning."
                    )

            if start_key_for_new_vertex_cycle and _singleton_instance.vertex_api_keys:
                try:
                    target_idx = _singleton_instance.vertex_api_keys.index(
                        start_key_for_new_vertex_cycle
                    )
                    for _ in range(target_idx):
                        next(_singleton_instance.vertex_key_cycle)
                    logger.info(
                        f"Vertex key cycle in new instance advanced. Next call to get_next_vertex_key() will yield: {start_key_for_new_vertex_cycle}"
                    )
                except ValueError:
                    logger.warning(
                        f"Determined start key '{start_key_for_new_vertex_cycle}' not found in new Vertex Express API keys during cycle advancement. "
                        "New cycle will start from the beginning."
                    )
                except StopIteration:
                    logger.error(
                        "StopIteration while advancing Vertex key cycle, implies empty new Vertex Express API key list previously missed."
                    )
                except Exception as e:
                    logger.error(
                        f"Error advancing new Vertex key cycle: {e}. Cycle will start from beginning."
                    )
            else:
                if _singleton_instance.vertex_api_keys:
                    logger.info(
                        "New Vertex key cycle will start from the beginning of the new Vertex Express API key list (no specific start key determined or needed)."
                    )
                else:
                    logger.info(
                        "New Vertex key cycle not applicable as the new Vertex Express API key list is empty."
                    )

            # 清理所有保存的状态
            _preserved_vertex_old_api_keys_for_reset = None
            _preserved_vertex_next_key_in_cycle = None

        return _singleton_instance


async def reset_key_manager_instance():
    """
    重置 KeyManager 单例实例。
    将保存当前实例的状态（失败计数、旧 API keys、下一个 key 提示）
    以供下一次 get_key_manager_instance 调用时恢复。
    """
    global _singleton_instance, _preserved_failure_counts, _preserved_vertex_failure_counts, _preserved_old_api_keys_for_reset, _preserved_vertex_old_api_keys_for_reset, _preserved_next_key_in_cycle, _preserved_vertex_next_key_in_cycle
    async with _singleton_lock:
        if _singleton_instance:
            # 1. 保存失败计数
            _preserved_failure_counts = _singleton_instance.key_failure_counts.copy()
            _preserved_vertex_failure_counts = (
                _singleton_instance.vertex_key_failure_counts.copy()
            )

            # 2. 保存旧的 API keys 列表
            _preserved_old_api_keys_for_reset = _singleton_instance.api_keys.copy()
            _preserved_vertex_old_api_keys_for_reset = (
                _singleton_instance.vertex_api_keys.copy()
            )

            # 3. 保存 key_cycle 的下一个 key 提示
            try:
                if _singleton_instance.api_keys:
                    _preserved_next_key_in_cycle = (
                        await _singleton_instance.get_next_key()
                    )
                else:
                    _preserved_next_key_in_cycle = None
            except StopIteration:
                logger.warning(
                    "Could not preserve next key hint: key cycle was empty or exhausted in old instance."
                )
                _preserved_next_key_in_cycle = None
            except Exception as e:
                logger.error(f"Error preserving next key hint during reset: {e}")
                _preserved_next_key_in_cycle = None

            # 4. 保存 vertex_key_cycle 的下一个 key 提示
            try:
                if _singleton_instance.vertex_api_keys:
                    _preserved_vertex_next_key_in_cycle = (
                        await _singleton_instance.get_next_vertex_key()
                    )
                else:
                    _preserved_vertex_next_key_in_cycle = None
            except StopIteration:
                logger.warning(
                    "Could not preserve next key hint: Vertex key cycle was empty or exhausted in old instance."
                )
                _preserved_vertex_next_key_in_cycle = None
            except Exception as e:
                logger.error(f"Error preserving next key hint during reset: {e}")
                _preserved_vertex_next_key_in_cycle = None

            _singleton_instance = None
            logger.info(
                "KeyManager instance has been reset. State (failure counts, old keys, next key hint) preserved for next instantiation."
            )
        else:
            logger.info(
                "KeyManager instance was not set (or already reset), no reset action performed."
            )
