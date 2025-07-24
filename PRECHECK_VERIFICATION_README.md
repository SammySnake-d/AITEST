# 预检机制验证指南

本指南提供了完整的预检机制验证方法，帮助你确认预检功能是否正常运行。

## 🎯 验证目标

验证预检机制是否能够：
1. ✅ 正确识别429错误并冻结密钥
2. ✅ 正确识别400等错误并标记密钥无效
3. ✅ 只向有效密钥发送请求
4. ✅ 减少日志中的429和400错误

## 📋 验证方法

### 方法1: 自动化完整验证 (推荐)

使用提供的自动化脚本进行完整验证：

```bash
# 1. 确保在项目根目录
cd gemini-balance增强/AITEST

# 2. 给脚本添加执行权限
chmod +x run_precheck_verification.sh

# 3. 运行完整验证
./run_precheck_verification.sh
```

这个脚本会：
- 启动专门的测试容器
- 检查预检配置
- 模拟API请求
- 分析日志
- 生成验证报告

### 方法2: 手动验证

#### 步骤1: 启动测试容器

```bash
# 使用专门的测试配置启动
docker-compose -f docker-compose-precheck-test.yml up -d

# 查看容器状态
docker ps | grep precheck

# 查看启动日志
docker-compose -f docker-compose-precheck-test.yml logs -f
```

#### 步骤2: 检查预检配置

```bash
# 运行预检验证脚本
python3 verify_precheck.py

# 或者手动检查API
curl http://localhost:8005/gemini/v1beta/precheck-config | python3 -m json.tool
```

#### 步骤3: 分析日志

```bash
# 运行日志分析脚本
python3 analyze_precheck_logs.py

# 或者手动查看日志
docker logs gemini-balance-aitest-precheck | grep -i precheck
```

### 方法3: 生产环境验证

如果你想在现有的生产容器中验证：

```bash
# 1. 检查预检配置
curl http://your-server:port/gemini/v1beta/precheck-config

# 2. 分析现有容器日志
python3 analyze_precheck_logs.py your-container-name

# 3. 监控密钥状态变化
curl http://your-server:port/openai/v1/keys/list
```

## 🔍 验证指标

### 预检机制正常工作的标志：

1. **配置检查**：
   - `enabled: true` - 预检已启用
   - `count > 0` - 预检数量大于0
   - `current_batch_valid_count > 0` - 当前批次有有效密钥

2. **日志分析**：
   - 看到 "Starting precheck" 日志
   - 看到 "Precheck triggered" 日志
   - 429错误后看到 "frozen due to 429 error" 日志
   - 400错误后看到 "marked as invalid" 日志

3. **密钥状态**：
   - 有效密钥数量稳定或增加
   - 无效/冻结密钥数量根据实际情况变化
   - 没有大量的429/400错误日志

### 预检机制异常的标志：

1. **配置问题**：
   - `enabled: false` - 预检未启用
   - `current_batch_valid_count: 0` - 没有有效密钥

2. **日志问题**：
   - 大量429/400错误但没有对应的冻结/标记无效日志
   - 没有看到预检相关日志
   - 错误日志持续增加

## 📊 验证报告解读

验证脚本会生成详细报告，关注以下关键指标：

```
📈 统计信息:
   预检执行次数: > 0        # 应该大于0
   429错误冻结密钥数: X     # 如果有429错误，应该有对应的冻结
   标记为无效密钥数: Y      # 如果有400错误，应该有对应的标记
   API 429错误数: Z        # 应该很少或为0
   API 400错误数: W        # 应该很少或为0
```

### 理想情况：
- 预检执行次数 > 0
- 如果有API错误，有对应的处理（冻结/标记无效）
- API错误数量很少

### 问题情况：
- 预检执行次数 = 0（预检未运行）
- 有API错误但没有对应处理（预检未生效）
- API错误数量很多（预检未有效防止）

## 🛠️ 故障排除

### 问题1: 预检未启用
```bash
# 检查环境变量
docker exec container-name env | grep PRECHECK

# 检查配置
curl http://localhost:8005/gemini/v1beta/precheck-config
```

### 问题2: 预检未触发
```bash
# 检查密钥数量是否足够
curl http://localhost:8005/openai/v1/keys/list

# 检查是否有足够的API请求
# 预检需要一定数量的请求才会触发
```

### 问题3: 日志中仍有错误
```bash
# 检查错误是否来自预检之前的请求
# 查看错误发生的时间和预检启动时间

# 检查是否所有密钥都已被预检
docker logs container-name | grep -i "precheck.*position"
```

## 📝 配置调优

如果预检机制工作但效果不理想，可以调整以下参数：

```env
# 增加预检数量
KEY_PRECHECK_COUNT=100

# 降低触发比例（更频繁预检）
KEY_PRECHECK_TRIGGER_RATIO=0.5

# 启用密钥冻结
ENABLE_KEY_FREEZE_ON_429=true

# 调整失败阈值
MAX_FAILURES=5
```

## 🎉 验证成功标准

预检机制验证成功的标准：

1. ✅ 预检配置正确启用
2. ✅ 预检定期执行
3. ✅ 429错误被正确处理（冻结密钥）
4. ✅ 400等错误被正确处理（标记无效）
5. ✅ API错误日志显著减少
6. ✅ 系统稳定运行

如果满足以上条件，说明预检机制正常工作，能够有效防止向无效密钥发送请求。
