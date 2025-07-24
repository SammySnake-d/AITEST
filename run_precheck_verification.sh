#!/bin/bash

# 预检机制完整验证脚本
# 使用方法: ./run_precheck_verification.sh

set -e

echo "🚀 开始预检机制完整验证流程"
echo "=================================="

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置
CONTAINER_NAME="gemini-balance-aitest-precheck"
COMPOSE_FILE="docker-compose-precheck-test.yml"
SERVICE_URL="http://localhost:8005"

# 函数：打印带颜色的消息
print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# 函数：检查依赖
check_dependencies() {
    print_info "检查依赖..."
    
    if ! command -v docker &> /dev/null; then
        print_error "Docker 未安装"
        exit 1
    fi
    
    if ! command -v docker-compose &> /dev/null; then
        print_error "Docker Compose 未安装"
        exit 1
    fi
    
    if ! command -v python3 &> /dev/null; then
        print_error "Python3 未安装"
        exit 1
    fi
    
    print_success "依赖检查通过"
}

# 函数：启动服务
start_service() {
    print_info "启动预检测试服务..."
    
    # 停止可能存在的容器
    docker-compose -f $COMPOSE_FILE down 2>/dev/null || true
    
    # 启动服务
    docker-compose -f $COMPOSE_FILE up -d
    
    print_info "等待服务启动..."
    sleep 30
    
    # 检查容器状态
    if ! docker ps | grep -q $CONTAINER_NAME; then
        print_error "容器启动失败"
        docker-compose -f $COMPOSE_FILE logs
        exit 1
    fi
    
    print_success "服务启动成功"
}

# 函数：等待服务就绪
wait_for_service() {
    print_info "等待服务就绪..."
    
    local max_attempts=30
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s $SERVICE_URL/health > /dev/null 2>&1; then
            print_success "服务就绪"
            return 0
        fi
        
        print_info "等待服务就绪... ($attempt/$max_attempts)"
        sleep 10
        ((attempt++))
    done
    
    print_error "服务启动超时"
    docker-compose -f $COMPOSE_FILE logs
    exit 1
}

# 函数：运行API验证
run_api_verification() {
    print_info "运行API验证..."
    
    if python3 verify_precheck.py; then
        print_success "API验证通过"
    else
        print_warning "API验证失败，继续进行日志分析"
    fi
}

# 函数：运行日志分析
run_log_analysis() {
    print_info "运行日志分析..."
    
    if python3 analyze_precheck_logs.py $CONTAINER_NAME; then
        print_success "日志分析完成"
    else
        print_warning "日志分析失败"
    fi
}

# 函数：生成验证报告
generate_report() {
    print_info "生成验证报告..."
    
    local report_file="precheck_verification_report_$(date +%Y%m%d_%H%M%S).txt"
    
    {
        echo "预检机制验证报告"
        echo "生成时间: $(date)"
        echo "=================================="
        echo ""
        
        echo "1. 容器状态:"
        docker ps | grep $CONTAINER_NAME || echo "容器未运行"
        echo ""
        
        echo "2. 容器健康检查:"
        docker inspect $CONTAINER_NAME | grep -A 10 '"Health"' || echo "无健康检查信息"
        echo ""
        
        echo "3. 最近日志 (最后100行):"
        docker logs --tail 100 $CONTAINER_NAME 2>&1
        echo ""
        
        echo "4. 预检配置检查:"
        curl -s $SERVICE_URL/gemini/v1beta/precheck-config | python3 -m json.tool 2>/dev/null || echo "无法获取预检配置"
        echo ""
        
        echo "5. 密钥状态检查:"
        curl -s $SERVICE_URL/openai/v1/keys/list | python3 -m json.tool 2>/dev/null || echo "无法获取密钥状态"
        
    } > $report_file
    
    print_success "验证报告已生成: $report_file"
}

# 函数：清理资源
cleanup() {
    print_info "清理资源..."
    
    # 询问是否保留容器
    read -p "是否保留测试容器以便进一步调试? (y/N): " -n 1 -r
    echo
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        docker-compose -f $COMPOSE_FILE down
        print_success "容器已清理"
    else
        print_info "容器已保留，可使用以下命令查看日志:"
        echo "  docker logs -f $CONTAINER_NAME"
        echo "  docker-compose -f $COMPOSE_FILE logs -f"
    fi
}

# 主流程
main() {
    echo "开始时间: $(date)"
    echo ""
    
    # 检查依赖
    check_dependencies
    
    # 启动服务
    start_service
    
    # 等待服务就绪
    wait_for_service
    
    # 运行验证
    echo ""
    print_info "开始验证流程..."
    
    # API验证
    run_api_verification
    
    # 等待一段时间让预检机制运行
    print_info "等待预检机制运行..."
    sleep 60
    
    # 日志分析
    run_log_analysis
    
    # 生成报告
    generate_report
    
    echo ""
    print_success "验证流程完成!"
    
    # 清理
    cleanup
}

# 错误处理
trap 'print_error "脚本执行失败"; cleanup; exit 1' ERR

# 运行主流程
main "$@"
