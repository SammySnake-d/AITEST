#!/usr/bin/env python3
"""
预检日志分析脚本
分析Docker容器日志中的预检相关信息
"""

import subprocess
import re
import json
from datetime import datetime
from collections import defaultdict

class PrecheckLogAnalyzer:
    def __init__(self, container_name="gemini-balance-aitest-precheck"):
        self.container_name = container_name
        
    def get_container_logs(self, lines=1000):
        """获取容器日志"""
        try:
            cmd = ["docker", "logs", "--tail", str(lines), self.container_name]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                return result.stdout + result.stderr
            else:
                print(f"❌ 获取容器日志失败: {result.stderr}")
                return None
        except subprocess.TimeoutExpired:
            print("❌ 获取容器日志超时")
            return None
        except Exception as e:
            print(f"❌ 获取容器日志异常: {e}")
            return None
    
    def analyze_precheck_logs(self, logs):
        """分析预检相关日志"""
        if not logs:
            return None
        
        analysis = {
            "precheck_events": [],
            "key_freeze_events": [],
            "key_invalid_events": [],
            "error_events": [],
            "api_requests": [],
            "statistics": {
                "total_precheck_runs": 0,
                "keys_frozen_by_429": 0,
                "keys_marked_invalid": 0,
                "api_429_errors": 0,
                "api_400_errors": 0,
                "api_success_requests": 0
            }
        }
        
        lines = logs.split('\n')
        
        for line in lines:
            if not line.strip():
                continue
            
            # 提取时间戳
            timestamp_match = re.search(r'(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})', line)
            timestamp = timestamp_match.group(1) if timestamp_match else "unknown"
            
            # 预检相关事件
            if "precheck" in line.lower():
                if "starting precheck" in line.lower():
                    analysis["precheck_events"].append({
                        "timestamp": timestamp,
                        "type": "precheck_start",
                        "message": line.strip()
                    })
                    analysis["statistics"]["total_precheck_runs"] += 1
                elif "precheck triggered" in line.lower():
                    analysis["precheck_events"].append({
                        "timestamp": timestamp,
                        "type": "precheck_triggered",
                        "message": line.strip()
                    })
                elif "precheck detected invalid key" in line.lower():
                    analysis["precheck_events"].append({
                        "timestamp": timestamp,
                        "type": "precheck_invalid_key",
                        "message": line.strip()
                    })
                elif "precheck confirmed key validity" in line.lower():
                    analysis["precheck_events"].append({
                        "timestamp": timestamp,
                        "type": "precheck_valid_key",
                        "message": line.strip()
                    })
            
            # 密钥冻结事件
            if "frozen due to 429 error" in line.lower():
                analysis["key_freeze_events"].append({
                    "timestamp": timestamp,
                    "type": "key_frozen_429",
                    "message": line.strip()
                })
                analysis["statistics"]["keys_frozen_by_429"] += 1
            
            # 密钥标记为无效事件
            if "marked as invalid" in line.lower():
                analysis["key_invalid_events"].append({
                    "timestamp": timestamp,
                    "type": "key_marked_invalid",
                    "message": line.strip()
                })
                analysis["statistics"]["keys_marked_invalid"] += 1
            
            # API错误事件
            if "429" in line and ("error" in line.lower() or "too many requests" in line.lower()):
                analysis["error_events"].append({
                    "timestamp": timestamp,
                    "type": "api_429_error",
                    "message": line.strip()
                })
                analysis["statistics"]["api_429_errors"] += 1
            
            if "400" in line and "error" in line.lower():
                analysis["error_events"].append({
                    "timestamp": timestamp,
                    "type": "api_400_error",
                    "message": line.strip()
                })
                analysis["statistics"]["api_400_errors"] += 1
            
            # API成功请求
            if "200" in line and ("chat/completions" in line or "generateContent" in line):
                analysis["api_requests"].append({
                    "timestamp": timestamp,
                    "type": "api_success",
                    "message": line.strip()
                })
                analysis["statistics"]["api_success_requests"] += 1
        
        return analysis
    
    def print_analysis_report(self, analysis):
        """打印分析报告"""
        if not analysis:
            print("❌ 无法分析日志")
            return
        
        print("📊 预检日志分析报告")
        print("=" * 60)
        
        # 统计信息
        stats = analysis["statistics"]
        print("📈 统计信息:")
        print(f"   预检执行次数: {stats['total_precheck_runs']}")
        print(f"   429错误冻结密钥数: {stats['keys_frozen_by_429']}")
        print(f"   标记为无效密钥数: {stats['keys_marked_invalid']}")
        print(f"   API 429错误数: {stats['api_429_errors']}")
        print(f"   API 400错误数: {stats['api_400_errors']}")
        print(f"   API 成功请求数: {stats['api_success_requests']}")
        
        # 预检事件
        if analysis["precheck_events"]:
            print(f"\n🔍 预检事件 ({len(analysis['precheck_events'])} 个):")
            for event in analysis["precheck_events"][-10:]:  # 显示最近10个
                print(f"   [{event['timestamp']}] {event['type']}: {event['message'][:100]}...")
        
        # 密钥冻结事件
        if analysis["key_freeze_events"]:
            print(f"\n🧊 密钥冻结事件 ({len(analysis['key_freeze_events'])} 个):")
            for event in analysis["key_freeze_events"][-5:]:  # 显示最近5个
                print(f"   [{event['timestamp']}] {event['message'][:100]}...")
        
        # 密钥无效事件
        if analysis["key_invalid_events"]:
            print(f"\n❌ 密钥无效事件 ({len(analysis['key_invalid_events'])} 个):")
            for event in analysis["key_invalid_events"][-5:]:  # 显示最近5个
                print(f"   [{event['timestamp']}] {event['message'][:100]}...")
        
        # 错误事件
        if analysis["error_events"]:
            print(f"\n⚠️  错误事件 ({len(analysis['error_events'])} 个):")
            for event in analysis["error_events"][-10:]:  # 显示最近10个
                print(f"   [{event['timestamp']}] {event['type']}: {event['message'][:100]}...")
        
        # 分析结论
        print("\n🎯 分析结论:")
        
        if stats['total_precheck_runs'] > 0:
            print("✅ 预检机制正在运行")
        else:
            print("❌ 未检测到预检机制运行")
        
        if stats['api_429_errors'] > 0 and stats['keys_frozen_by_429'] == 0:
            print("⚠️  发现429错误但未冻结密钥，可能预检机制未正常工作")
        elif stats['keys_frozen_by_429'] > 0:
            print("✅ 预检机制正确处理了429错误")
        
        if stats['api_400_errors'] > 0 and stats['keys_marked_invalid'] == 0:
            print("⚠️  发现400错误但未标记密钥无效，可能预检机制未正常工作")
        elif stats['keys_marked_invalid'] > 0:
            print("✅ 预检机制正确处理了400等错误")
        
        if stats['api_429_errors'] == 0 and stats['api_400_errors'] == 0:
            print("✅ 未发现API错误，预检机制可能有效防止了无效请求")
        
        print("\n" + "=" * 60)
    
    def run_analysis(self):
        """运行分析"""
        print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 开始分析预检日志")
        print(f"📦 容器名称: {self.container_name}")
        
        # 获取日志
        print("📥 获取容器日志...")
        logs = self.get_container_logs(2000)  # 获取最近2000行日志
        
        if not logs:
            print("❌ 无法获取日志")
            return False
        
        print(f"✅ 成功获取日志 ({len(logs.split())} 行)")
        
        # 分析日志
        print("🔍 分析日志内容...")
        analysis = self.analyze_precheck_logs(logs)
        
        # 打印报告
        self.print_analysis_report(analysis)
        
        return True

def main():
    """主函数"""
    import sys
    
    container_name = "gemini-balance-aitest-precheck"
    if len(sys.argv) > 1:
        container_name = sys.argv[1]
    
    analyzer = PrecheckLogAnalyzer(container_name)
    
    try:
        success = analyzer.run_analysis()
        if success:
            print("✅ 日志分析完成")
        else:
            print("❌ 日志分析失败")
    except KeyboardInterrupt:
        print("\n⚠️  分析被用户中断")
    except Exception as e:
        print(f"❌ 分析过程中发生异常: {e}")

if __name__ == "__main__":
    main()
