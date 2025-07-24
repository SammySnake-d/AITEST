#!/usr/bin/env python3
"""
预检机制验证脚本
用于验证预检机制是否正常运行
"""

import requests
import time
import json
import sys
from datetime import datetime

class PrecheckVerifier:
    def __init__(self, base_url="http://localhost:8005"):
        self.base_url = base_url
        self.session = requests.Session()
        
    def check_health(self):
        """检查服务健康状态"""
        try:
            resp = self.session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except Exception as e:
            print(f"❌ 健康检查失败: {e}")
            return False
    
    def get_precheck_config(self):
        """获取预检配置"""
        try:
            resp = self.session.get(f"{self.base_url}/gemini/v1beta/precheck-config", timeout=10)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ 获取预检配置失败: {resp.status_code}")
                return None
        except Exception as e:
            print(f"❌ 获取预检配置异常: {e}")
            return None
    
    def get_keys_status(self):
        """获取密钥状态"""
        try:
            resp = self.session.get(f"{self.base_url}/openai/v1/keys/list", timeout=10)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"❌ 获取密钥状态失败: {resp.status_code}")
                return None
        except Exception as e:
            print(f"❌ 获取密钥状态异常: {e}")
            return None
    
    def simulate_requests(self, count=10):
        """模拟API请求以触发预检"""
        print(f"🔄 模拟 {count} 个API请求以触发预检...")
        
        for i in range(count):
            try:
                # 模拟聊天请求
                payload = {
                    "model": "gemini-1.5-flash",
                    "messages": [{"role": "user", "content": f"Test message {i+1}"}],
                    "max_tokens": 10
                }
                
                resp = self.session.post(
                    f"{self.base_url}/openai/v1/chat/completions",
                    json=payload,
                    timeout=30
                )
                
                print(f"   请求 {i+1}: {resp.status_code}")
                time.sleep(1)  # 避免请求过快
                
            except Exception as e:
                print(f"   请求 {i+1} 失败: {e}")
    
    def verify_precheck_mechanism(self):
        """验证预检机制"""
        print("🚀 开始验证预检机制...")
        print("=" * 60)
        
        # 1. 检查服务健康状态
        print("1️⃣ 检查服务健康状态...")
        if not self.check_health():
            print("❌ 服务不健康，无法继续验证")
            return False
        print("✅ 服务健康")
        
        # 2. 获取预检配置
        print("\n2️⃣ 检查预检配置...")
        config = self.get_precheck_config()
        if not config:
            print("❌ 无法获取预检配置")
            return False
        
        print(f"✅ 预检配置:")
        print(f"   启用状态: {config.get('enabled', False)}")
        print(f"   预检数量: {config.get('count', 0)}")
        print(f"   触发比例: {config.get('trigger_ratio', 0)}")
        print(f"   当前批次大小: {config.get('current_batch_size', 0)}")
        print(f"   当前批次有效密钥数: {config.get('current_batch_valid_count', 0)}")
        print(f"   已使用有效密钥数: {config.get('valid_keys_passed_count', 0)}")
        print(f"   触发阈值: {config.get('valid_keys_trigger_threshold', 0)}")
        
        if not config.get('enabled', False):
            print("❌ 预检机制未启用")
            return False
        
        # 3. 获取密钥状态
        print("\n3️⃣ 检查密钥状态...")
        keys_status = self.get_keys_status()
        if not keys_status:
            print("❌ 无法获取密钥状态")
            return False
        
        data = keys_status.get('data', {})
        valid_count = len(data.get('valid_keys', {}))
        invalid_count = len(data.get('invalid_keys', {}))
        frozen_count = len(data.get('disabled_keys', {}))  # 实际是冻结列表
        
        print(f"✅ 密钥状态:")
        print(f"   有效密钥: {valid_count}")
        print(f"   无效密钥: {invalid_count}")
        print(f"   冻结密钥: {frozen_count}")
        print(f"   总密钥数: {keys_status.get('total', 0)}")
        
        if valid_count == 0:
            print("❌ 没有有效密钥，无法测试预检机制")
            return False
        
        # 4. 记录初始状态
        initial_config = config.copy()
        
        # 5. 模拟请求以触发预检
        print("\n4️⃣ 模拟API请求以触发预检...")
        self.simulate_requests(20)
        
        # 6. 等待预检执行
        print("\n5️⃣ 等待预检执行...")
        time.sleep(10)
        
        # 7. 检查预检是否被触发
        print("\n6️⃣ 检查预检执行结果...")
        new_config = self.get_precheck_config()
        if not new_config:
            print("❌ 无法获取更新后的预检配置")
            return False
        
        # 比较配置变化
        print(f"✅ 预检执行结果:")
        print(f"   初始已使用有效密钥数: {initial_config.get('valid_keys_passed_count', 0)}")
        print(f"   当前已使用有效密钥数: {new_config.get('valid_keys_passed_count', 0)}")
        print(f"   初始批次有效密钥数: {initial_config.get('current_batch_valid_count', 0)}")
        print(f"   当前批次有效密钥数: {new_config.get('current_batch_valid_count', 0)}")
        
        # 判断预检是否正常工作
        used_keys_increased = new_config.get('valid_keys_passed_count', 0) > initial_config.get('valid_keys_passed_count', 0)
        
        if used_keys_increased:
            print("✅ 预检机制正常工作 - 有效密钥使用计数增加")
        else:
            print("⚠️  预检机制可能未正常工作 - 有效密钥使用计数未增加")
        
        # 8. 检查最终密钥状态
        print("\n7️⃣ 检查最终密钥状态...")
        final_keys_status = self.get_keys_status()
        if final_keys_status:
            final_data = final_keys_status.get('data', {})
            final_valid = len(final_data.get('valid_keys', {}))
            final_invalid = len(final_data.get('invalid_keys', {}))
            final_frozen = len(final_data.get('disabled_keys', {}))
            
            print(f"✅ 最终密钥状态:")
            print(f"   有效密钥: {final_valid} (变化: {final_valid - valid_count:+d})")
            print(f"   无效密钥: {final_invalid} (变化: {final_invalid - invalid_count:+d})")
            print(f"   冻结密钥: {final_frozen} (变化: {final_frozen - frozen_count:+d})")
            
            # 如果有密钥状态变化，说明预检可能发现了问题
            if final_invalid > invalid_count or final_frozen > frozen_count:
                print("✅ 预检机制发现并处理了无效/问题密钥")
            else:
                print("ℹ️  预检期间未发现问题密钥")
        
        print("\n" + "=" * 60)
        print("🎉 预检机制验证完成!")
        
        return True

def main():
    """主函数"""
    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 开始预检机制验证")
    
    verifier = PrecheckVerifier()
    
    try:
        success = verifier.verify_precheck_mechanism()
        if success:
            print("✅ 验证成功完成")
            sys.exit(0)
        else:
            print("❌ 验证失败")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️  验证被用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 验证过程中发生异常: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
