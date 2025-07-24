#!/usr/bin/env python3
"""
测试预检机制的简单脚本
"""

import asyncio
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.service.key.key_manager import KeyManager
from app.config.config import settings

async def test_precheck_mechanism():
    """测试预检机制"""
    print("🔧 测试预检机制...")
    
    # 创建测试用的API keys
    test_keys = [
        "AIzaSyTest1_invalid_key_for_testing",
        "AIzaSyTest2_invalid_key_for_testing", 
        "AIzaSyTest3_invalid_key_for_testing",
        "AIzaSyTest4_invalid_key_for_testing",
        "AIzaSyTest5_invalid_key_for_testing"
    ]
    
    # 创建KeyManager实例
    key_manager = KeyManager(test_keys, [])
    
    print(f"📋 创建了包含 {len(test_keys)} 个测试密钥的KeyManager")
    print(f"⚙️  预检配置:")
    print(f"    启用: {key_manager.precheck_enabled}")
    print(f"    数量: {key_manager.precheck_count}")
    print(f"    触发比例: {key_manager.precheck_trigger_ratio}")
    print(f"    动态调整: {key_manager.precheck_dynamic_adjustment}")
    print(f"    安全缓冲比例: {key_manager.precheck_safety_buffer_ratio}")
    print(f"    最小保留比例: {key_manager.precheck_min_reserve_ratio}")
    print(f"    密钥倍数: {key_manager.precheck_min_keys_multiplier}")
    print(f"    估计并发: {key_manager.precheck_estimated_concurrent}")
    
    # 测试获取下一个密钥
    print("\n🔄 测试密钥轮询...")
    for i in range(3):
        key = await key_manager.get_next_working_key()
        print(f"  第{i+1}次获取密钥: {key[:20]}...")
    
    # 测试预检密钥获取
    print("\n🔍 测试预检密钥获取...")
    precheck_keys = key_manager._get_precheck_keys(0, 3)
    print(f"  获取到预检 {len(precheck_keys)} 个密钥:")
    for i, key in enumerate(precheck_keys):
        print(f"    {i+1}. {key[:20]}...")

    # 测试预检配置更新（简化版本）
    print("\n⚙️  测试预检配置更新...")
    key_manager.update_precheck_config(enabled=True, count=50, trigger_ratio=0.5)
    print(f"  更新后配置: 启用={key_manager.precheck_enabled}, 数量={key_manager.precheck_count}, 触发比例={key_manager.precheck_trigger_ratio}")

    # 模拟密钥使用以触发预检
    print("\n🔄 模拟密钥使用以测试预检触发...")
    print(f"  当前批次有效密钥位置: {key_manager.current_batch_valid_keys}")
    print(f"  有效密钥总数: {key_manager.current_batch_valid_count}")
    print(f"  触发阈值: {key_manager.valid_keys_trigger_threshold}")

    for i in range(12):  # 增加测试次数以观察批次切换
        key = await key_manager.get_next_working_key()
        current_absolute_position = (key_manager.key_usage_counter - 1) % len(key_manager.api_keys)
        is_valid_position = current_absolute_position in key_manager.current_batch_valid_keys

        print(f"  第{i+1}次使用密钥: {key[:20]}...")
        print(f"    指针位置: {key_manager.key_usage_counter}, 绝对位置: {current_absolute_position}")
        print(f"    是否有效位置: {is_valid_position}, 已过有效密钥: {key_manager.valid_keys_passed_count}/{key_manager.current_batch_valid_count}")
        print(f"    下一批次状态: 就绪={key_manager.next_batch_ready}, 数量={key_manager.next_batch_valid_count}")

        await asyncio.sleep(0.5)  # 短暂等待

    # 等待预检完成
    print("\n⏳ 等待预检操作完成...")
    await asyncio.sleep(5)
    
    print("\n✅ 预检机制测试完成!")

if __name__ == "__main__":
    # 临时设置预检配置
    settings.KEY_PRECHECK_ENABLED = True
    settings.KEY_PRECHECK_COUNT = 5
    settings.KEY_PRECHECK_TRIGGER_RATIO = 0.5
    settings.KEY_PRECHECK_MIN_KEYS_MULTIPLIER = 1  # 降低要求以便测试
    settings.KEY_PRECHECK_ESTIMATED_CONCURRENT_REQUESTS = 2
    settings.KEY_PRECHECK_DYNAMIC_ADJUSTMENT = True
    settings.KEY_PRECHECK_SAFETY_BUFFER_RATIO = 1.5
    settings.KEY_PRECHECK_MIN_RESERVE_RATIO = 0.3

    asyncio.run(test_precheck_mechanism())
