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
        self.disabled_keys: set = set()  # 禁用的密钥
        self.disabled_vertex_keys: set = set()  # 禁用的Vertex密钥
        self.frozen_keys: Dict[str, datetime] = {}  # 冷冻的密钥及其解冻时间
        self.frozen_vertex_keys: Dict[str, datetime] = {}  # 冷冻的Vertex密钥及其解冻时间
        self.key_state_lock = asyncio.Lock()  # 密钥状态锁
        self.vertex_key_state_lock = asyncio.Lock()  # Vertex密钥状态锁

        # 预检机制（简化配置）
        self.precheck_enabled = settings.KEY_PRECHECK_ENABLED
        self.precheck_count = settings.KEY_PRECHECK_COUNT
        self.precheck_trigger_ratio = settings.KEY_PRECHECK_TRIGGER_RATIO
        self.precheck_lock = asyncio.Lock()  # 预检锁

        # 固定的内部参数（不再通过配置暴露）
        self.precheck_min_keys_multiplier = 5  # 密钥数量必须是并发数的5倍才启用预检
        self.precheck_estimated_concurrent = 20  # 估计的每秒并发请求数
        self.precheck_dynamic_adjustment = True  # 启用动态调整
        self.precheck_safety_buffer_ratio = 1.5  # 安全缓冲比例
        self.precheck_min_reserve_ratio = 0.3  # 最小保留比例

        # 预检状态跟踪（基于有效密钥比例）
        self.precheck_current_batch_size = 0  # 当前批次的预检数量
        self.precheck_in_progress = False  # 是否正在进行预检
        self.precheck_last_position = 0  # 上次预检的结束位置
        self.precheck_base_position = 0  # 当前预检批次的起始位置
        self.key_usage_counter = 0  # 密钥使用计数器（原始指针位置）

        # 有效密钥跟踪
        self.current_batch_valid_keys = []  # 当前预检批次中的有效密钥位置列表
        self.current_batch_valid_count = 0  # 当前批次中有效密钥的总数
        self.valid_keys_trigger_threshold = 0  # 触发下一次预检需要经过的有效密钥数量
        self.valid_keys_passed_count = 0  # 已经经过的有效密钥数量

        # 下一批次队列管理
        self.next_batch_valid_keys = []  # 下一批次的有效密钥位置列表
        self.next_batch_valid_count = 0  # 下一批次中有效密钥的总数
        self.next_batch_ready = False  # 下一批次是否准备就绪

        # API调用统计缓存
        self.last_minute_calls = 0  # 上一分钟的调用次数
        self.stats_update_time = datetime.now()  # 统计更新时间

        # 检查是否应该启用预检
        if self._should_enable_precheck():
            self._calculate_precheck_trigger()
            logger.info(f"Precheck enabled: count={self.precheck_count}, trigger_ratio={self.precheck_trigger_ratio}, min_keys_required={self.precheck_min_keys_multiplier * self.precheck_estimated_concurrent}")
        else:
            self.precheck_enabled = False
            logger.info(f"Precheck disabled: insufficient keys ({len(self.api_keys)}) for estimated concurrent requests ({self.precheck_estimated_concurrent})")

        # 执行初始预检
        if self.precheck_enabled and self.api_keys:
            asyncio.create_task(self._perform_initial_precheck())

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
        """获取下一可用的API key"""
        initial_key = await self.get_next_key()
        current_key = initial_key

        while True:
            if await self.is_key_valid(current_key):
                # 只有在找到有效密钥时才进行预检触发检查
                if self.precheck_enabled:
                    await self._check_precheck_trigger_for_valid_key()
                return current_key

            current_key = await self.get_next_key()
            if current_key == initial_key:
                return current_key

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
        disabled_keys = {}

        async with self.failure_count_lock:
            for key in self.api_keys:
                fail_count = self.key_failure_counts[key]

                # 获取密钥状态信息
                is_disabled = await self.is_key_disabled(key)
                is_frozen = await self.is_key_frozen(key)

                key_info = {
                    "fail_count": fail_count,
                    "disabled": is_disabled,
                    "frozen": is_frozen
                }

                if is_disabled:
                    # 禁用的密钥单独分类
                    disabled_keys[key] = key_info
                elif fail_count < self.MAX_FAILURES:
                    # 有效密钥（未禁用且失败次数未达到上限）
                    valid_keys[key] = key_info
                else:
                    # 无效密钥（失败次数达到上限但未被禁用）
                    invalid_keys[key] = key_info

        return {
            "valid_keys": valid_keys,
            "invalid_keys": invalid_keys,
            "disabled_keys": disabled_keys
        }

    async def get_keys_by_status_paginated(
        self,
        key_type: str = "valid",
        page: int = 1,
        page_size: int = 10,
        search: str = None,
        fail_count_threshold: int = 0
    ) -> dict:
        """获取分页的API key列表"""
        # 首先获取所有密钥状态
        all_keys_status = await self.get_keys_by_status()

        # 根据类型选择对应的密钥
        if key_type == "valid":
            target_keys = all_keys_status["valid_keys"]
        elif key_type == "invalid":
            target_keys = all_keys_status["invalid_keys"]
        elif key_type == "disabled":
            target_keys = all_keys_status["disabled_keys"]
        else:
            raise ValueError(f"Invalid key_type: {key_type}")

        # 转换为列表以便处理
        keys_list = []
        for key, key_info in target_keys.items():
            # 确保key_info是字典格式
            if isinstance(key_info, dict):
                fail_count = key_info.get("fail_count", 0)
                disabled = key_info.get("disabled", False)
                frozen = key_info.get("frozen", False)
            else:
                # 兼容旧格式（直接是失败次数）
                fail_count = key_info
                disabled = await self.is_key_disabled(key)
                frozen = await self.is_key_frozen(key)

            keys_list.append({
                "key": key,
                "fail_count": fail_count,
                "disabled": disabled,
                "frozen": frozen
            })

        # 应用搜索过滤
        if search:
            search_lower = search.lower()
            keys_list = [
                item for item in keys_list
                if search_lower in item["key"].lower()
            ]

        # 应用失败次数阈值过滤（仅对valid类型有效）
        if key_type == "valid" and fail_count_threshold > 0:
            keys_list = [
                item for item in keys_list
                if item["fail_count"] >= fail_count_threshold
            ]

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

        # 转换回字典格式以保持兼容性
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
            logger.info(f"Key {key} frozen until {freeze_until}")
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
        """解冻指定密钥"""
        async with self.key_state_lock:
            if key in self.frozen_keys:
                del self.frozen_keys[key]
                logger.info(f"Key {key} unfrozen")
                return True
            return False

    async def unfreeze_vertex_key(self, key: str) -> bool:
        """解冻指定Vertex密钥"""
        async with self.vertex_key_state_lock:
            if key in self.frozen_vertex_keys:
                del self.frozen_vertex_keys[key]
                logger.info(f"Vertex key {key} unfrozen")
                return True
            return False

    async def is_key_frozen(self, key: str) -> bool:
        """检查密钥是否被冷冻"""
        async with self.key_state_lock:
            if key not in self.frozen_keys:
                return False

            # 检查是否已过期，如果过期则自动解冻
            if datetime.now() >= self.frozen_keys[key]:
                del self.frozen_keys[key]
                logger.info(f"Key {key} auto-unfrozen (freeze period expired)")
                return False
            return True

    async def is_vertex_key_frozen(self, key: str) -> bool:
        """检查Vertex密钥是否被冷冻"""
        async with self.vertex_key_state_lock:
            if key not in self.frozen_vertex_keys:
                return False

            # 检查是否已过期，如果过期则自动解冻
            if datetime.now() >= self.frozen_vertex_keys[key]:
                del self.frozen_vertex_keys[key]
                logger.info(f"Vertex key {key} auto-unfrozen (freeze period expired)")
                return False
            return True

    # 密钥禁用管理方法
    async def disable_key(self, key: str) -> bool:
        """禁用指定密钥"""
        async with self.key_state_lock:
            self.disabled_keys.add(key)
            logger.info(f"Key {key} disabled")
            return True

    async def disable_vertex_key(self, key: str) -> bool:
        """禁用指定Vertex密钥"""
        async with self.vertex_key_state_lock:
            self.disabled_vertex_keys.add(key)
            logger.info(f"Vertex key {key} disabled")
            return True

    async def enable_key(self, key: str) -> bool:
        """启用指定密钥"""
        async with self.key_state_lock:
            if key in self.disabled_keys:
                self.disabled_keys.remove(key)
                logger.info(f"Key {key} enabled")
                return True
            return False

    async def enable_vertex_key(self, key: str) -> bool:
        """启用指定Vertex密钥"""
        async with self.vertex_key_state_lock:
            if key in self.disabled_vertex_keys:
                self.disabled_vertex_keys.remove(key)
                logger.info(f"Vertex key {key} enabled")
                return True
            return False

    async def is_key_disabled(self, key: str) -> bool:
        """检查密钥是否被禁用"""
        async with self.key_state_lock:
            return key in self.disabled_keys

    async def is_vertex_key_disabled(self, key: str) -> bool:
        """检查Vertex密钥是否被禁用"""
        async with self.vertex_key_state_lock:
            return key in self.disabled_vertex_keys

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
        if not settings.ENABLE_KEY_FREEZE_ON_429:
            return False

        if is_vertex:
            await self.freeze_vertex_key(api_key)
            logger.warning(f"Vertex key {api_key} frozen due to 429 error")
        else:
            await self.freeze_key(api_key)
            logger.warning(f"Key {api_key} frozen due to 429 error")
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
        """检查是否应该启用预检机制"""
        if not self.api_keys:
            return False

        min_required_keys = self.precheck_estimated_concurrent * self.precheck_min_keys_multiplier
        return len(self.api_keys) >= min_required_keys

    async def _check_precheck_trigger_for_valid_key(self):
        """检查是否需要触发预检（仅在使用有效密钥时调用）"""
        if not self.precheck_enabled:
            return

        # 首先检查是否需要切换批次
        if self._check_and_switch_batch():
            logger.info("Switched to new precheck batch")

        # 检查当前指针位置是否是有效密钥位置
        current_absolute_position = (self.key_usage_counter - 1) % len(self.api_keys)  # -1因为counter已经递增

        # 检查当前位置是否在当前批次的有效密钥列表中
        if current_absolute_position in self.current_batch_valid_keys:
            self.valid_keys_passed_count += 1
            logger.debug(f"Used valid key at position {current_absolute_position}, count: {self.valid_keys_passed_count}/{self.valid_keys_trigger_threshold}")

            # 检查是否达到触发阈值（且当前没有预检在进行）
            if (self.valid_keys_passed_count >= self.valid_keys_trigger_threshold and
                not self.precheck_in_progress):
                logger.info(f"Precheck triggered: used {self.valid_keys_passed_count} valid keys out of {self.current_batch_valid_count} total valid keys (threshold: {self.valid_keys_trigger_threshold})")
                logger.info(f"Continuing to use remaining {self.current_batch_valid_count - self.valid_keys_passed_count} valid keys while precheck runs in background")
                # 在后台异步执行预检，不阻塞当前请求，也不立即重置状态
                asyncio.create_task(self._perform_precheck_async())

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

                # 如果启用动态调整，重新计算预检参数
                if self.precheck_dynamic_adjustment:
                    await self._adjust_precheck_parameters()

                logger.debug(f"Updated API call stats: {self.last_minute_calls} calls in last minute")
        except Exception as e:
            logger.error(f"Failed to update API call stats: {e}")

    async def _adjust_precheck_parameters(self):
        """基于API调用统计动态调整预检参数"""
        if not self.precheck_enabled or self.last_minute_calls <= 0:
            return

        # 计算建议的预检数量（基于每分钟调用数 + 安全缓冲）
        suggested_precheck_count = int(self.last_minute_calls * self.precheck_safety_buffer_ratio)

        # 限制在合理范围内
        min_precheck = max(10, self.last_minute_calls)
        max_precheck = min(1000, len(self.api_keys) // 2)
        suggested_precheck_count = max(min_precheck, min(suggested_precheck_count, max_precheck))

        # 如果建议值与当前值差异较大，则调整
        if abs(suggested_precheck_count - self.precheck_current_batch_size) > self.precheck_current_batch_size * 0.2:
            old_size = self.precheck_current_batch_size
            self.precheck_current_batch_size = suggested_precheck_count
            self._calculate_precheck_trigger()
            logger.info(f"Adjusted precheck batch size: {old_size} -> {self.precheck_current_batch_size} (based on {self.last_minute_calls} calls/min)")

    def _calculate_precheck_trigger(self):
        """计算预检触发阈值（基于有效密钥数量）"""
        if not self.precheck_enabled or self.current_batch_valid_count == 0:
            self.valid_keys_trigger_threshold = 0
            return

        # 基于有效密钥数量计算触发阈值
        self.valid_keys_trigger_threshold = int(self.current_batch_valid_count * self.precheck_trigger_ratio)

        # 确保至少保留最小比例的有效密钥
        min_reserve = int(self.current_batch_valid_count * self.precheck_min_reserve_ratio)
        if self.valid_keys_trigger_threshold > self.current_batch_valid_count - min_reserve:
            self.valid_keys_trigger_threshold = self.current_batch_valid_count - min_reserve

        # 确保触发阈值至少为1（如果有有效密钥的话）
        self.valid_keys_trigger_threshold = max(1, self.valid_keys_trigger_threshold)

        logger.debug(f"Precheck trigger calculated: valid_keys_trigger_threshold={self.valid_keys_trigger_threshold}, valid_count={self.current_batch_valid_count}, ratio={self.precheck_trigger_ratio}, reserve={min_reserve}")

    async def _check_precheck_safety(self) -> bool:
        """检查预检安全性（确保剩余有效密钥足够）"""
        if not self.precheck_enabled:
            return True

        # 计算剩余的有效密钥数量
        remaining_valid_keys = self.current_batch_valid_count - self.valid_keys_passed_count

        # 检查剩余有效密钥数量是否足够应对当前的调用频率
        if self.last_minute_calls > 0:
            # 预估需要的有效密钥数量（考虑1分钟的调用量）
            estimated_needed = min(self.last_minute_calls, self.current_batch_valid_count)

            if remaining_valid_keys < estimated_needed:
                logger.warning(f"Precheck safety check failed: remaining_valid_keys={remaining_valid_keys}, needed={estimated_needed}")
                return False

        return True

    async def _perform_initial_precheck(self):
        """执行初始预检"""
        try:
            # 初始化预检参数
            self.precheck_current_batch_size = self.precheck_count
            self._calculate_precheck_trigger()
            await self._update_api_call_stats()
            await self._perform_precheck_async()
        except Exception as e:
            logger.error(f"Error in initial precheck: {e}")

    async def _perform_precheck_async(self):
        """异步执行预检操作"""
        async with self.precheck_lock:
            if self.precheck_in_progress:
                return

            self.precheck_in_progress = True

        try:
            # 更新API调用统计
            await self._update_api_call_stats()

            # 检查预检安全性
            if not await self._check_precheck_safety():
                logger.warning("Precheck safety check failed, triggering immediate precheck")

            await self._perform_precheck()
        finally:
            async with self.precheck_lock:
                self.precheck_in_progress = False

    async def _perform_precheck(self):
        """执行预检操作"""
        if not self.precheck_enabled or not self.api_keys:
            return

        try:
            # 确定预检数量
            batch_size = self.precheck_current_batch_size or self.precheck_count

            # 计算预检起始位置（从上次结束位置开始）
            start_index = self.precheck_last_position
            keys_to_check = self._get_precheck_keys(start_index, batch_size)

            if not keys_to_check:
                return

            logger.info(f"Starting precheck: start_index={start_index}, checking {len(keys_to_check)} keys, batch_size={batch_size}")

            # 并发检查这些密钥，同时记录位置
            tasks = []
            key_positions = []
            for i, key in enumerate(keys_to_check):
                position = (start_index + i) % len(self.api_keys)
                key_positions.append(position)
                task = asyncio.create_task(self._precheck_single_key_with_position(key, position))
                tasks.append(task)

            # 等待所有检查完成
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 统计结果并收集有效密钥位置
            valid_positions = []
            valid_count = 0
            invalid_count = 0
            error_count = 0

            for i, result in enumerate(results):
                if result is True:
                    valid_count += 1
                    valid_positions.append(key_positions[i])
                elif result is False:
                    invalid_count += 1
                elif isinstance(result, Exception):
                    error_count += 1

            logger.info(f"Precheck completed: {valid_count} valid, {invalid_count} invalid, {error_count} errors")
            logger.info(f"Valid key positions: {valid_positions}")

            # 更新预检状态
            self.precheck_last_position = (start_index + batch_size) % len(self.api_keys)

            # 只有在这是新的预检批次时才重置状态
            # 如果当前批次还有剩余有效密钥，则延迟状态重置
            if self._should_switch_to_new_batch(valid_positions, valid_count):
                logger.info("Switching to new precheck batch")
                self.precheck_base_position = self.key_usage_counter  # 更新基准位置

                # 更新有效密钥跟踪信息
                self.current_batch_valid_keys = valid_positions
                self.current_batch_valid_count = valid_count
                self.valid_keys_passed_count = 0  # 重置已经过的有效密钥计数

                # 重新计算触发阈值
                self._calculate_precheck_trigger()
            else:
                logger.info("Precheck completed, but continuing with current batch until exhausted")
                # 将新的有效密钥位置添加到待用列表中
                self._queue_next_batch_keys(valid_positions, valid_count)

            # 记录预检效果
            if valid_count > 0:
                logger.info(f"Precheck prepared {valid_count} valid keys at positions {valid_positions}")
                if self.current_batch_valid_count > 0:
                    logger.info(f"Current batch: {self.valid_keys_passed_count}/{self.current_batch_valid_count} valid keys used, trigger threshold: {self.valid_keys_trigger_threshold}")
            else:
                logger.warning("No valid keys found in current precheck batch")

        except Exception as e:
            logger.error(f"Error in precheck operation: {e}")

    def _should_switch_to_new_batch(self, new_valid_positions: List[int], new_valid_count: int) -> bool:
        """判断是否应该切换到新的预检批次"""
        # 如果当前批次已经用完所有有效密钥，则切换到新批次
        if self.valid_keys_passed_count >= self.current_batch_valid_count:
            logger.debug("Current batch exhausted, switching to new batch")
            return True

        # 如果当前批次还有剩余有效密钥，则不切换
        remaining_valid_keys = self.current_batch_valid_count - self.valid_keys_passed_count
        logger.debug(f"Current batch has {remaining_valid_keys} remaining valid keys, delaying batch switch")
        return False

    def _queue_next_batch_keys(self, valid_positions: List[int], valid_count: int):
        """将新预检的有效密钥加入下一批次队列"""
        self.next_batch_valid_keys = valid_positions
        self.next_batch_valid_count = valid_count
        self.next_batch_ready = True
        logger.info(f"Queued next batch: {valid_count} valid keys at positions {valid_positions}")

    def _check_and_switch_batch(self):
        """检查是否需要切换到下一批次"""
        # 如果当前批次用完且下一批次准备就绪，则切换
        if (self.valid_keys_passed_count >= self.current_batch_valid_count and
            self.next_batch_ready):

            logger.info(f"Switching from exhausted batch to queued batch: {self.next_batch_valid_count} valid keys")

            # 切换到下一批次
            self.current_batch_valid_keys = self.next_batch_valid_keys
            self.current_batch_valid_count = self.next_batch_valid_count
            self.valid_keys_passed_count = 0  # 重置计数
            self.precheck_base_position = self.key_usage_counter  # 更新基准位置

            # 清空下一批次队列
            self.next_batch_valid_keys = []
            self.next_batch_valid_count = 0
            self.next_batch_ready = False

            # 重新计算触发阈值
            self._calculate_precheck_trigger()

            return True
        return False

    def _get_precheck_keys(self, start_index: int, count: int) -> List[str]:
        """获取预检密钥列表"""
        if not self.api_keys or count <= 0:
            return []

        keys = []
        for i in range(count):
            index = (start_index + i) % len(self.api_keys)
            keys.append(self.api_keys[index])

        return keys



    async def _precheck_single_key(self, key: str):
        """预检单个密钥（兼容性方法）"""
        return await self._precheck_single_key_with_position(key, -1)

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
                # 增加失败计数
                async with self.failure_count_lock:
                    self.key_failure_counts[key] += 1
                    if self.key_failure_counts[key] >= self.MAX_FAILURES:
                        logger.warning(f"Key {redact_key_for_logging(key)} marked as invalid after precheck (fail count: {self.key_failure_counts[key]})")
                return False
            else:
                logger.debug(f"Precheck confirmed key validity: {redact_key_for_logging(key)} at position {position}")
                return True

        except Exception as e:
            logger.error(f"Error prechecking key {redact_key_for_logging(key)} at position {position}: {e}")
            return None  # 返回None表示检查出错

    async def _validate_key_with_api(self, key: str) -> bool:
        """使用API验证密钥有效性"""
        try:
            import aiohttp
            import json

            # 构造验证请求
            url = f"{settings.BASE_URL}/models"
            headers = {
                "x-goog-api-key": key,
                "Content-Type": "application/json"
            }

            timeout = aiohttp.ClientTimeout(total=10)  # 10秒超时

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        return True
                    elif response.status == 429:
                        # 429错误，冷冻密钥
                        if settings.ENABLE_KEY_FREEZE_ON_429:
                            await self.freeze_key(key)
                            logger.warning(f"Key {redact_key_for_logging(key)} frozen due to 429 error during precheck")
                        return False
                    else:
                        logger.debug(f"Key validation failed with status {response.status}: {redact_key_for_logging(key)}")
                        return False

        except asyncio.TimeoutError:
            logger.debug(f"Key validation timeout: {redact_key_for_logging(key)}")
            return False
        except Exception as e:
            logger.debug(f"Key validation error: {redact_key_for_logging(key)}, error: {e}")
            return False

    def update_precheck_config(self, enabled: bool = None, count: int = None, trigger_ratio: float = None):
        """更新预检配置（简化版本，只支持核心参数）"""
        config_changed = False

        if enabled is not None and enabled != self.precheck_enabled:
            self.precheck_enabled = enabled
            config_changed = True

        if count is not None and count != self.precheck_count:
            self.precheck_count = max(10, count)  # 最小为10
            config_changed = True

        if trigger_ratio is not None and trigger_ratio != self.precheck_trigger_ratio:
            self.precheck_trigger_ratio = max(0.1, min(1.0, trigger_ratio))  # 限制在0.1-1.0之间
            config_changed = True

        if config_changed:
            logger.info(f"Precheck config updated: enabled={self.precheck_enabled}, count={self.precheck_count}, trigger_ratio={self.precheck_trigger_ratio}, dynamic={self.precheck_dynamic_adjustment}")

            # 重新检查是否应该启用预检
            if self.precheck_enabled and not self._should_enable_precheck():
                self.precheck_enabled = False
                logger.warning("Precheck disabled due to insufficient keys for estimated concurrent requests")
            elif self.precheck_enabled:
                # 重新计算触发点
                self.precheck_current_batch_size = self.precheck_count
                self._calculate_precheck_trigger()
                # 如果当前没有在进行预检，立即执行一次
                if not self.precheck_in_progress:
                    asyncio.create_task(self._perform_precheck_async())


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
