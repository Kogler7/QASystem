#!/usr/bin/env python3
"""
机车检修问答意图预测演示程序
基于 qasystem-wzc-dev 项目训练的 Qwen/Qwen3-8B 模型

数据集: 中文机车检修问答 (510训练样本, 8个意图类别)
意图类别:
  1. 参数标准 - 技术参数、数值范围、标准值
  2. 部件检查 - 检查方法、状态要求、外观检测
  3. 故障处理 - 问题解决、故障修复、处理方法
  4. 操作规程 - 操作步骤、程序要求、规范流程
  5. 试验要求 - 测试方法、试验条件、验证标准
  6. 通用知识 - 原理说明、概念定义、基础知识
  7. 维修限度 - 磨损限值、更换标准、报废条件
  8. 安装要求 - 装配规范、安装位置、固定要求

使用方法:
    # 交互式模式
    python predict_intent.py
    
    # 单次预测
    python predict_intent.py --question "轴承径向游隙的标准范围是多少？"
    
    # 批量预测
    python predict_intent.py --batch questions.txt

要求: vLLM API 需要在 http://127.0.0.1:8000 运行
"""

import os
import sys
import re
import json
import argparse
import requests
from pathlib import Path
from typing import Dict, List, Optional


class TrackIntentPredictor:
    """机车检修问答意图预测器"""
    
    # 8个真实意图类别
    INTENT_CATEGORIES = [
        "参数标准",
        "部件检查", 
        "故障处理",
        "操作规程",
        "试验要求",
        "通用知识",
        "维修限度",
        "安装要求"
    ]
    
    # Few-shot 示例 (从真实数据中提取)
    FEW_SHOT_EXAMPLES = """示例1:
问题: 轴承径向游隙的标准范围是多少？
意图: 参数标准

示例2:
问题: 检查空气干燥器筒时，对上盖和筒体焊缝有何要求？
意图: 部件检查

示例3:
问题: 制动缸体内表面出现拉伤应如何处理？
意图: 故障处理

示例4:
问题: 柴油机主要紧固件的紧固力矩应遵循哪个表格的规定？
意图: 操作规程

示例5:
问题: 绝缘介电试验合格的标准是什么？
意图: 试验要求

示例6:
问题: 柴油机的工作原理是什么？
意图: 通用知识

示例7:
问题: 端盖轴承孔的圆柱度中修限度与禁用限度有何区别？
意图: 维修限度

示例8:
问题: 万向轴组装时两端叉头安装十字轴孔的中心线应在什么位置？
意图: 安装要求"""
    
    def __init__(self, 
                 api_url: str = 'http://127.0.0.1:8000/v1/completions',
                 model: str = 'Qwen/Qwen3-8B',
                 temperature: float = 0.12):
        """
        初始化预测器
        
        Args:
            api_url: vLLM API URL
            model: 模型名称
            temperature: 采样温度
        """
        self.api_url = api_url
        self.model = model
        self.temperature = temperature
        
        print("=" * 70)
        print("机车检修问答意图预测系统")
        print("=" * 70)
        print(f"模型: {model}")
        print(f"API: {api_url}")
        print(f"意图类别数: {len(self.INTENT_CATEGORIES)}")
        print("\n已加载意图类别:")
        for i, intent in enumerate(self.INTENT_CATEGORIES, 1):
            print(f"  {i}. {intent}")
        print("=" * 70)
        print()
    
    def _create_prompt(self, question: str) -> str:
        """创建分类提示词"""
        intent_list = "、".join(self.INTENT_CATEGORIES)
        
        prompt = f"""你是机车检修领域的问答分类专家。请将用户的问题准确分类到以下8个意图类别之一。

**意图类别:**
{intent_list}

**分类规则:**
1. 仔细分析问题的核心关注点
2. 问题涉及具体数值、范围、标准值 → 参数标准
3. 问题涉及检查方法、状态判断 → 部件检查
4. 问题涉及故障、问题、修复 → 故障处理
5. 问题涉及操作步骤、程序 → 操作规程
6. 问题涉及测试、试验、验证 → 试验要求
7. 问题涉及原理、定义、概念 → 通用知识
8. 问题涉及磨损限值、报废标准 → 维修限度
9. 问题涉及装配、安装、固定 → 安装要求

**参考示例:**
{self.FEW_SHOT_EXAMPLES}

**用户问题:**
{question}

**请按以下格式输出分类结果:**
意图: <从8个类别中选择一个>"""
        
        return prompt
    
    def predict(self, question: str) -> Dict:
        """
        预测问题的意图
        
        Args:
            question: 用户问题
            
        Returns:
            包含预测结果的字典
        """
        if not question or not question.strip():
            return {
                'question': question,
                'intent': 'INVALID',
                'confidence': 'N/A',
                'error': '输入为空'
            }
        
        question = question.strip()
        prompt = self._create_prompt(question)
        
        try:
            # 调用 vLLM API
            payload = {
                "model": self.model,
                "prompt": prompt,
                "max_tokens": 50,
                "temperature": self.temperature,
                "stop": ["\n\n", "用户问题:", "**"]
            }
            
            response = requests.post(
                self.api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            
            if response.status_code != 200:
                return {
                    'question': question,
                    'intent': 'API_ERROR',
                    'confidence': 'N/A',
                    'error': f"API返回错误: HTTP {response.status_code}"
                }
            
            result = response.json()
            output = result.get('choices', [{}])[0].get('text', '').strip()
            
            # 解析意图
            intent = self._parse_intent(output)
            
            # 验证是否为已知类别
            if intent in self.INTENT_CATEGORIES:
                confidence = "HIGH"
            elif any(cat in output for cat in self.INTENT_CATEGORIES):
                # 输出中包含已知类别但解析失败
                for cat in self.INTENT_CATEGORIES:
                    if cat in output:
                        intent = cat
                        confidence = "MEDIUM"
                        break
            else:
                confidence = "LOW"
            
            return {
                'question': question,
                'intent': intent,
                'confidence': confidence,
                'raw_output': output
            }
            
        except requests.exceptions.ConnectionError:
            return {
                'question': question,
                'intent': 'CONNECTION_ERROR',
                'confidence': 'N/A',
                'error': f"无法连接到API: {self.api_url}\n请确保vLLM服务正在运行"
            }
        except requests.exceptions.Timeout:
            return {
                'question': question,
                'intent': 'TIMEOUT',
                'confidence': 'N/A',
                'error': "请求超时"
            }
        except Exception as e:
            return {
                'question': question,
                'intent': 'ERROR',
                'confidence': 'N/A',
                'error': str(e)
            }
    
    def _parse_intent(self, output: str) -> str:
        """从模型输出中解析意图"""
        # 方法1: 提取 "意图: xxx"
        match = re.search(r'意图[:：]\s*([^\n,，]+)', output)
        if match:
            intent = match.group(1).strip()
            # 检查是否为有效类别
            for cat in self.INTENT_CATEGORIES:
                if cat in intent:
                    return cat
            return intent
        
        # 方法2: 直接匹配类别名称
        for cat in self.INTENT_CATEGORIES:
            if cat in output:
                return cat
        
        # 方法3: 提取最后一个有意义的词
        words = [w.strip() for w in output.split() if w.strip()]
        if words:
            return words[-1]
        
        return "未知"
    
    def print_result(self, result: Dict):
        """打印预测结果"""
        print("\n" + "=" * 70)
        print("【预测结果】")
        print("=" * 70)
        print(f"问题: {result['question']}")
        print(f"意图: {result['intent']}")
        print(f"置信度: {result['confidence']}")
        
        if 'error' in result:
            print(f"错误: {result['error']}")
        elif result['intent'] in self.INTENT_CATEGORIES:
            idx = self.INTENT_CATEGORIES.index(result['intent']) + 1
            print(f"类别: {idx}/{len(self.INTENT_CATEGORIES)}")
        
        print("=" * 70)
    
    def run_interactive(self):
        """交互式预测模式"""
        print("\n【交互式模式】(输入 'quit' 或 'exit' 退出)\n")
        print("示例问题:")
        examples = [
            "轴承径向游隙的标准范围是多少？",
            "制动缸体内表面出现拉伤应如何处理？",
            "绝缘介电试验合格的标准是什么？"
        ]
        for ex in examples:
            print(f"  • {ex}")
        print()
        
        while True:
            try:
                question = input("\n请输入问题: ").strip()
                
                if question.lower() in ['quit', 'exit', 'q', '退出']:
                    print("\n再见!")
                    break
                
                if not question:
                    print("⚠ 输入不能为空")
                    continue
                
                print("正在预测...", end='', flush=True)
                result = self.predict(question)
                print("\r" + " " * 20 + "\r", end='')  # 清除"正在预测..."
                
                self.print_result(result)
                
            except KeyboardInterrupt:
                print("\n\n检测到中断,退出程序")
                break
            except Exception as e:
                print(f"\n错误: {e}")
    
    def run_batch(self, input_file: str, output_file: Optional[str] = None):
        """批量预测模式"""
        if not os.path.exists(input_file):
            print(f"错误: 文件不存在 - {input_file}")
            return
        
        print(f"\n【批量预测模式】")
        print(f"输入文件: {input_file}")
        
        # 读取问题
        with open(input_file, 'r', encoding='utf-8') as f:
            questions = [line.strip() for line in f if line.strip()]
        
        print(f"共 {len(questions)} 个问题\n")
        
        results = []
        for i, question in enumerate(questions, 1):
            print(f"[{i}/{len(questions)}] 预测中...", end='\r')
            result = self.predict(question)
            results.append(result)
        
        print(" " * 50 + "\r", end='')  # 清除进度
        
        # 打印结果
        print("\n" + "=" * 70)
        print("预测结果汇总")
        print("=" * 70)
        for i, result in enumerate(results, 1):
            # Use ASCII characters for better compatibility
            status_map = {
                "HIGH": "H",
                "MEDIUM": "M",
                "LOW": "L"
            }
            status = status_map.get(result['confidence'], "?")
            print(f"{i:2d}. [{status}] {result['intent']:8s} | {result['question']}")
        print("=" * 70)
        
        # 统计
        intent_counts = {}
        for result in results:
            intent = result['intent']
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
        
        print("\n统计信息:")
        for intent in self.INTENT_CATEGORIES:
            count = intent_counts.get(intent, 0)
            if count > 0:
                print(f"  {intent}: {count}")
        
        # 保存结果
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"\n结果已保存到: {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='机车检修问答意图预测',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python predict_intent.py
  python predict_intent.py -q "轴承径向游隙的标准范围是多少？"
  python predict_intent.py --batch questions.txt -o results.json
        """
    )
    
    parser.add_argument('-q', '--question', type=str,
                       help='单次预测: 输入问题')
    parser.add_argument('-b', '--batch', type=str,
                       help='批量预测: 输入文件路径 (每行一个问题)')
    parser.add_argument('-o', '--output', type=str,
                       help='批量预测结果输出文件 (JSON格式)')
    parser.add_argument('--api-url', type=str, 
                       default='http://127.0.0.1:8000/v1/completions',
                       help='vLLM API地址 (默认: http://127.0.0.1:8000/v1/completions)')
    parser.add_argument('--model', type=str, default='Qwen/Qwen3-8B',
                       help='模型名称 (默认: Qwen/Qwen3-8B)')
    parser.add_argument('--temperature', type=float, default=0.12,
                       help='采样温度 (默认: 0.12)')
    
    args = parser.parse_args()
    
    # 初始化预测器
    try:
        predictor = TrackIntentPredictor(
            api_url=args.api_url,
            model=args.model,
            temperature=args.temperature
        )
    except Exception as e:
        print(f"初始化失败: {e}")
        sys.exit(1)
    
    # 运行模式
    if args.question:
        # 单次预测
        result = predictor.predict(args.question)
        predictor.print_result(result)
    elif args.batch:
        # 批量预测
        predictor.run_batch(args.batch, args.output)
    else:
        # 交互式
        predictor.run_interactive()


if __name__ == "__main__":
    main()