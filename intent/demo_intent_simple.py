#!/usr/bin/env python3
"""
IntentGPT 意图预测演示程序 (简化版)
基于 qasystem-wzc-dev 项目的机车检修问答数据集训练结果
使用 vLLM API 进行意图识别

使用方法:
    # 交互式模式
    python demo_intent_simple.py
    
    # 单次预测模式
    python demo_intent_simple.py --question "轴承径向游隙的标准范围是多少？"
"""

import os
import sys
import re
import json
import argparse
import requests
from pathlib import Path
import glob


class IntentPredictor:
    """意图预测器 - 基于训练好的模型和发现的意图类别"""
    
    @staticmethod
    def _find_latest_discovered_intents():
        """自动查找最新的 discovered_categories.txt 文件"""
        pattern = 'results_v3/track/*/discovered_categories.txt'
        files = glob.glob(pattern)
        if not files:
            return None
        # 按修改时间排序,返回最新的
        latest = max(files, key=os.path.getmtime)
        return latest
    
    def __init__(self,
                 discovered_intents_path=None,
                 api_url='http://127.0.0.1:8000/v1/completions',
                 model='Qwen/Qwen3-8B',
                 temperature=0.12):
        """
        初始化意图预测器
        
        Args:
            discovered_intents_path: 已发现意图类别文件路径
            api_url: vLLM API URL
            model: 模型名称
            temperature: 模型温度参数
        """
        print("=" * 60)
        print("IntentGPT 意图预测系统")
        print("=" * 60)
        
        # 加载已发现的意图类别
        self.known_intents = self._load_discovered_intents(discovered_intents_path)
        print(f"已加载 {len(self.known_intents)} 个已发现的问答意图类别")
        
        # vLLM API 配置
        self.api_url = api_url
        self.model = model
        self.temperature = temperature
        
        print(f"\n连接 vLLM API: {api_url}")
        print(f"模型: {model}")
        print("系统初始化完成!\n")
        print("=" * 60)
        
    def _load_discovered_intents(self, path):
        """加载已发现的意图类别"""
        if not os.path.exists(path):
            print(f"警告: 找不到意图类别文件: {path}")
            print("使用默认问答意图类别...")
            return self._get_default_intents()
        
        with open(path, 'r', encoding='utf-8') as f:
            intents = []
            for line in f:
                line = line.strip()
                # 过滤掉无效的意图行(包含特殊字符的错误数据)
                if line and not line.startswith('s:') and not line.startswith('s_') and ',' not in line[:50]:
                    intents.append(line)
        
        # 去重并排序
        intents = sorted(list(set(intents)))
        return intents if intents else self._get_default_intents()
    
    def _get_default_intents(self):
        """获取默认意图类别"""
        return [
            "参数标准", "部件检查", "故障处理", "操作规程",
            "试验要求", "通用知识", "维修限度", "安装要求"
        ]
    
    def _create_prompt(self, user_input):
        """创建Prompt"""
        few_shot_examples = """问题: 轴承径向游隙的标准范围是多少？, 意图: 参数标准
问题: 检查空气干燥器筒时，对上盖和筒体焊缝有何要求？, 意图: 部件检查
问题: 制动缸体内表面出现拉伤应如何处理？, 意图: 故障处理
问题: 柴油机主要紧固件的紧固力矩应遵循哪个表格的规定？, 意图: 操作规程
问题: 绝缘介电试验合格的标准是什么？, 意图: 试验要求
问题: 柴油机的工作原理是什么？, 意图: 通用知识
问题: 端盖轴承孔的圆柱度中修限度与禁用限度有何区别？, 意图: 维修限度
问题: 万向轴组装时两端叉头安装十字轴孔的中心线应在什么位置？, 意图: 安装要求"""

        known_intents_str = ', '.join(self.known_intents[:50])
        
        prompt = f"""你是机车检修问答分类专家。

**任务**: 将用户的问题分类到已知的意图类别中。

**指导原则:**
1. 识别用户问题的核心需求
2. 尽可能匹配已知意图
3. 如果问题涉及数值范围、技术参数，通常是"参数标准"
4. 如果问题涉及检查方法、状态要求，通常是"部件检查"
5. 如果问题涉及问题解决、故障修复，通常是"故障处理"

**已知意图类别:**
{known_intents_str}

**示例:**
{few_shot_examples}

**用户问题:**
{user_input}

**你的回答 (格式: "问题: <输入>, 意图: <意图>"):**
"""
        return prompt
    
    def predict(self, user_input):
        """
        预测用户输入的意图
        
        Args:
            user_input: 用户问题/utterance
            
        Returns:
            dict: {'utterance': str, 'intent': str, 'confidence': str}
        """
        if not user_input or not user_input.strip():
            return {'utterance': user_input, 'intent': 'INVALID_INPUT', 'confidence': 'N/A'}
        
        prompt = self._create_prompt(user_input.strip())
        
        try:
            print("正在分析意图...", end='', flush=True)
            
            # 调用 vLLM API
            payload = {
                "model": self.model,
                "prompt": prompt,
                "max_tokens": 100,
                "temperature": self.temperature,
                "stop": ["\n\n", "User Input:", "**"]
            }
            
            response = requests.post(
                self.api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=60
            )
            
            if response.status_code != 200:
                print(f" API错误: {response.status_code}")
                return {
                    'utterance': user_input.strip(),
                    'intent': 'API_ERROR',
                    'confidence': 'N/A',
                    'error': f"HTTP {response.status_code}"
                }
            
            result = response.json()
            output = result.get('choices', [{}])[0].get('text', '')
            print(" 完成")
            
            # 解析输出
            intent = self._parse_intent(output)
            
            # 检查是否为已知意图
            confidence = "HIGH (Known)" if intent in self.known_intents else "MEDIUM (New)"
            
            return {
                'utterance': user_input.strip(),
                'intent': intent,
                'confidence': confidence,
                'raw_output': output
            }
            
        except requests.exceptions.ConnectionError:
            print(" 连接失败")
            return {
                'utterance': user_input.strip(),
                'intent': 'CONNECTION_ERROR',
                'confidence': 'N/A',
                'error': f"无法连接到 vLLM API: {self.api_url}"
            }
        except Exception as e:
            print(f" 错误: {str(e)}")
            return {
                'utterance': user_input.strip(),
                'intent': 'ERROR',
                'confidence': 'N/A',
                'error': str(e)
            }
    
    def _parse_intent(self, output):
        """解析LLM输出中的意图"""
        # 尝试提取 "意图: xxx" 格式 (中文)
        match = re.search(r'意图:\s*([^\n,，]+)', output)
        if match:
            return match.group(1).strip()
        
        # 尝试提取 "Intent: xxx" 格式 (英文)
        match = re.search(r'Intent:\s*([a-z_\-0-9]+)', output, re.IGNORECASE)
        if match:
            return match.group(1).strip().lower().replace(' ', '_')
        
        # 尝试提取JSON格式
        try:
            json_match = re.search(r'\{.*\}', output, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                if 'intent' in data:
                    return data['intent'].strip().lower().replace(' ', '_')
        except:
            pass
        
        # 回退: 提取最后一个有意义的词组
        words = output.strip().split()
        if words:
            last_word = words[-1].strip().lower().replace(' ', '_')
            last_word = re.sub(r'[^\w_]', '', last_word)
            return last_word if last_word else 'unknown'
        
        return 'unknown'
    
    def run_interactive(self):
        """运行交互式演示"""
        print("\n交互式意图预测 (输入 'quit' 或 'exit' 退出)")
        print("=" * 60)
        print("\n示例问题:")
        print("  - 轴承径向游隙的标准范围是多少？")
        print("  - 检查空气干燥器筒时，对上盖和筒体焊缝有何要求？")
        print("  - 制动缸体内表面出现拉伤应如何处理？")
        print("  - 柴油机主要紧固件的紧固力矩应遵循哪个表格的规定？")
        print("  - 绝缘介电试验合格的标准是什么？")
        print("  - 柴油机的工作原理是什么？")
        print()
        
        while True:
            try:
                user_input = input("\n请输入您的问题: ").strip()
                
                if user_input.lower() in ['quit', 'exit', 'q', '退出']:
                    print("\n再见!")
                    break
                
                if not user_input:
                    print("输入不能为空")
                    continue
                
                result = self.predict(user_input)
                
                print("\n" + "=" * 60)
                print("预测结果:")
                print("=" * 60)
                print(f"输入问题:  {result['utterance']}")
                print(f"预测意图:  {result['intent']}")
                print(f"置信度:    {result['confidence']}")
                
                if 'error' in result:
                    print(f"错误信息:  {result['error']}")
                
                if result['intent'] in self.known_intents:
                    print(f"该意图在已发现的{len(self.known_intents)}个类别中")
                elif result['intent'] not in ['ERROR', 'API_ERROR', 'CONNECTION_ERROR', 'INVALID_INPUT']:
                    print(f"这是一个新发现的意图!")
                
                print("=" * 60)
                
            except KeyboardInterrupt:
                print("\n\n检测到中断,退出程序")
                break
            except Exception as e:
                print(f"\n发生错误: {str(e)}")
                continue


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='IntentGPT 意图预测演示')
    parser.add_argument('--question', '-q', type=str, help='直接输入问题进行单次预测')
    parser.add_argument('--api-url', type=str, default='http://127.0.0.1:8000/v1/completions',
                       help='vLLM API URL (默认: http://127.0.0.1:8000/v1/completions)')
    parser.add_argument('--model', '-m', type=str, default='Qwen/Qwen3-8B',
                       help='模型名称 (默认: Qwen/Qwen3-8B)')
    parser.add_argument('--temperature', '-t', type=float, default=0.12,
                       help='模型温度参数 (默认: 0.12)')
    parser.add_argument('--intents-file', '-i', type=str,
                       default=None,
                       help='已发现意图类别文件路径 (默认: 自动查找最新结果)')
    
    args = parser.parse_args()
    
    # 从环境变量读取配置 (如果存在)
    api_url = os.getenv('VLLM_API_URL', args.api_url)
    model = os.getenv('VLLM_MODEL', args.model)
    
    try:
        predictor = IntentPredictor(
            discovered_intents_path=args.intents_file,
            api_url=api_url,
            model=model,
            temperature=args.temperature
        )
    except Exception as e:
        print(f"初始化失败: {str(e)}")
        print("\n确保 vLLM API 正在运行")
        return
    
    # 单次预测模式
    if args.question:
        print(f"\n输入问题: {args.question}")
        result = predictor.predict(args.question)
        
        print("\n" + "=" * 60)
        print("预测结果:")
        print("=" * 60)
        print(f"预测意图:  {result['intent']}")
        print(f"置信度:    {result['confidence']}")
        if 'error' in result:
            print(f"错误信息:  {result['error']}")
        print("=" * 60)
    else:
        predictor.run_interactive()


if __name__ == "__main__":
    main()