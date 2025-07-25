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
    print(f"    当前批次: {key_manager.current_batch_name}")
    print(f"    批次A准备状态: {key_manager.batch_a_ready}")
    print(f"    批次B准备状态: {key_manager.batch_b_ready}")
    
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
    await key_manager.update_precheck_config(enabled=True, count=50, trigger_ratio=0.5)
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

async def test_error_handling():
    """测试预检机制的错误处理"""
    print("\n🧪 测试预检机制的错误处理...")

    # 创建测试密钥
    test_keys = [
        "AIzaSyTest_Valid_Key_001",
        "AIzaSyTest_429_Error_Key",
        "AIzaSyTest_400_Error_Key",
        "AIzaSyTest_Valid_Key_002"
    ]

    key_manager = KeyManager(test_keys, [])

    print(f"📋 创建了包含 {len(test_keys)} 个测试密钥的KeyManager")
    print(f"⚙️  预检配置: 启用={key_manager.precheck_enabled}")

    # 测试不同错误类型的处理
    print("\n🔍 测试429错误处理...")
    test_429_key = "AIzaSyTest_429_Error_Key"

    # 模拟429错误
    if settings.ENABLE_KEY_FREEZE_ON_429:
        await key_manager.freeze_key(test_429_key)
        print(f"✅ 429错误密钥已冻结: {test_429_key}")
        is_frozen = await key_manager.is_key_frozen(test_429_key)
        print(f"   冻结状态: {is_frozen}")

    print("\n🔍 测试400错误处理...")
    test_400_key = "AIzaSyTest_400_Error_Key"

    # 模拟400错误（立即标记为无效）
    async with key_manager.failure_count_lock:
        key_manager.key_failure_counts[test_400_key] = key_manager.MAX_FAILURES

    is_valid = await key_manager.is_key_valid(test_400_key)
    print(f"✅ 400错误密钥已标记为无效: {test_400_key}")
    print(f"   有效状态: {is_valid}")

    # 测试密钥状态获取
    print("\n📊 测试密钥状态分类...")
    keys_status = await key_manager.get_keys_by_status()

    print(f"   有效密钥: {len(keys_status['valid_keys'])}")
    print(f"   无效密钥: {len(keys_status['invalid_keys'])}")
    print(f"   冻结密钥: {len(keys_status['frozen_keys'])}")

    # 显示详细状态
    for key, info in keys_status['frozen_keys'].items():
        print(f"     冻结: {key} (手动冻结: {info.get('manually_frozen', False)})")

    for key, info in keys_status['invalid_keys'].items():
        print(f"     无效: {key} (失败次数: {info.get('fail_count', 0)})")

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

    print("🚀 开始测试预检机制...")
    asyncio.run(test_precheck_mechanism())

    print("\n" + "="*50)
    print("🚀 开始测试错误处理...")
    asyncio.run(test_error_handling())
