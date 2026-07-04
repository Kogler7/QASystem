#!/bin/bash
# IntentGPT 意图预测演示运行脚本

# 设置颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=================================================${NC}"
echo -e "${GREEN}IntentGPT 意图预测演示${NC}"
echo -e "${GREEN}=================================================${NC}"

# 检查 Python 环境
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未找到 python3${NC}"
    exit 1
fi

# 检查必要的包
echo -e "\n${YELLOW}检查依赖...${NC}"
python3 -c "import langchain_core" 2>/dev/null
if [ $? -ne 0 ]; then
    echo -e "${YELLOW}缺少依赖包，正在安装...${NC}"
    pip install langchain-core langchain-community python-dotenv requests -q
fi

# 检查 .env 文件
if [ ! -f .env ]; then
    echo -e "${YELLOW}警告: 未找到 .env 文件，使用默认配置${NC}"
    echo "VLLM_API_URL=http://127.0.0.1:8000/v1/completions" > .env
    echo "VLLM_MODEL=Qwen/Qwen3-8B" >> .env
    echo "VLLM_API_KEY=" >> .env
fi

# 运行演示程序
echo -e "\n${GREEN}启动意图预测系统...${NC}\n"

if [ $# -eq 0 ]; then
    # 交互式模式
    python3 demo_intent_prediction.py
else
    # 单次预测模式
    python3 demo_intent_prediction.py --question "$*"
fi