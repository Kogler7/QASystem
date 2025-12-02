#!/usr/bin/env python3
"""
QASystem 统一意图识别接口

集成qasystem-wzc-dev项目的意图识别功能,提供简洁的Python接口。
用于识别机车检修问答的意图类别。

基本使用:
    >>> from intent import QAIntentSystem
    >>> system = QAIntentSystem()
    >>> result = system.predict("轴承径向游隙的标准范围是多少?")
    >>> print(result['intent'])  # "参数标准"

详细文档请参考: INTERFACE_DESIGN.md
"""

import os
import sys
import time
from typing import List, Dict, Optional, Any

# 添加intent目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
intent_dir = os.path.join(current_dir, 'intent')
if intent_dir not in sys.path:
    sys.path.insert(0, intent_dir)

# 导入预测器(避免模块名冲突)
from predict_intent import TrackIntentPredictor


class QAIntentSystem:
    """
    机车检修问答意图识别系统
    
    用于识别用户问题的意图类别,支持8个预定义类别:
    参数标准、部件检查、故障处理、操作规程、试验要求、
    通用知识、维修限度、安装要求
    
    Attributes:
        api_url: vLLM API地址
        model: 模型名称
        temperature: 采样温度
        timeout: API请求超时时间
        verbose: 是否输出详细日志
    
    Example:
        >>> system = QAIntentSystem()
        >>> result = system.predict("轴承径向游隙的标准范围是多少?")
        >>> if result['success']:
        ...     print(f"意图: {result['intent']}")
    """
    
    def __init__(
        self,
        api_url: str = 'http://127.0.0.1:8000/v1/completions',
        model: str = 'Qwen/Qwen3-8B',
        temperature: float = 0.12,
        timeout: int = 30,
        verbose: bool = False
    ):
        """
        初始化意图识别系统
        
        Args:
            api_url: vLLM API地址
            model: 模型名称
            temperature: 采样温度(0.0-1.0,越低越确定)
            timeout: API请求超时时间(秒)
            verbose: 是否输出详细日志
            
        Example:
            >>> system = QAIntentSystem()
            >>> system = QAIntentSystem(temperature=0.15, verbose=True)
        """
        self.api_url = api_url
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.verbose = verbose
        
        # 初始化统计信息
        self._stats = {
            'total_predictions': 0,
            'successful_predictions': 0,
            'failed_predictions': 0,
            'total_time': 0.0
        }
        
        # 初始化内部预测器
        try:
            if self.verbose:
                print(f"[INFO] 初始化QAIntentSystem")
                print(f"[INFO] API: {self.api_url}")
                print(f"[INFO] Model: {self.model}")
                print(f"[INFO] Temperature: {self.temperature}")
            
            self._predictor = TrackIntentPredictor(
                api_url=self.api_url,
                model=self.model,
                temperature=self.temperature
            )
            
            if self.verbose:
                print(f"[SUCCESS] 系统初始化完成")
                print(f"[INFO] 支持的意图类别: {len(self.get_intent_categories())}个")
                
        except Exception as e:
            print(f"[ERROR] 初始化失败: {str(e)}")
            raise
    
    def predict(
        self,
        question: str,
        return_confidence: bool = True,
        return_raw: bool = False
    ) -> Dict[str, Any]:
        """
        预测单个问题的意图
        
        Args:
            question: 用户问题
            return_confidence: 是否返回置信度
            return_raw: 是否返回原始模型输出
            
        Returns:
            {
                'question': str,           # 输入问题
                'intent': str,             # 预测意图
                'confidence': str,         # 置信度: HIGH/MEDIUM/LOW
                'category_id': int,        # 意图类别ID (1-8)
                'success': bool,           # 是否预测成功
                'error': str,              # 错误信息(如有)
                'raw_output': str,         # 原始输出(可选)
                'response_time': float     # 响应时间(秒)
            }
            
        Example:
            >>> result = system.predict("轴承径向游隙的标准范围是多少?")
            >>> print(result['intent'])  # "参数标准"
            >>> print(result['confidence'])  # "HIGH"
        """
        start_time = time.time()
        
        if self.verbose:
            print(f"[INFO] 预测问题: {question[:50]}...")
        
        try:
            # 调用内部预测器
            raw_result = self._predictor.predict(question)
            
            # 格式化结果
            result = self._format_result(raw_result, return_confidence, return_raw)
            
            # 计算响应时间
            response_time = time.time() - start_time
            result['response_time'] = round(response_time, 3)
            
            # 更新统计
            self._update_stats(response_time, result['success'])
            
            if self.verbose:
                if result['success']:
                    print(f"[SUCCESS] 预测完成: {result['intent']} (置信度: {result.get('confidence', 'N/A')})")
                else:
                    print(f"[ERROR] 预测失败: {result.get('error', 'Unknown error')}")
            
            return result
            
        except Exception as e:
            response_time = time.time() - start_time
            self._update_stats(response_time, False)
            
            error_result = {
                'question': question,
                'intent': 'ERROR',
                'confidence': 'N/A',
                'category_id': -1,
                'success': False,
                'error': f'UNEXPECTED_ERROR: {str(e)}',
                'response_time': round(response_time, 3)
            }
            
            if self.verbose:
                print(f"[ERROR] 异常: {str(e)}")
            
            return error_result
    
    def predict_batch(
        self,
        questions: List[str],
        show_progress: bool = True,
        continue_on_error: bool = True
    ) -> Dict[str, Any]:
        """
        批量预测多个问题的意图
        
        Args:
            questions: 问题列表
            show_progress: 是否显示进度
            continue_on_error: 遇到错误是否继续处理其他问题
            
        Returns:
            {
                'results': [
                    {'question': ..., 'intent': ..., 'success': True, ...},
                    ...
                ],
                'summary': {
                    'total': int,              # 总问题数
                    'success': int,            # 成功预测数
                    'failed': int,             # 失败数
                    'success_rate': float,     # 成功率(%)
                    'avg_response_time': float,# 平均响应时间(秒)
                    'intent_distribution': {   # 意图分布
                        '参数标准': 3,
                        '部件检查': 2,
                        ...
                    }
                }
            }
            
        Example:
            >>> questions = [
            ...     "轴承径向游隙的标准范围是多少?",
            ...     "检查空气干燥器筒时有何要求?",
            ...     "制动缸体内表面拉伤如何处理?"
            ... ]
            >>> result = system.predict_batch(questions)
            >>> print(f"成功率: {result['summary']['success_rate']:.1f}%")
        """
        if self.verbose or show_progress:
            print(f"[INFO] 开始批量预测 {len(questions)} 个问题...")
        
        results = []
        total_time = 0.0
        
        for i, question in enumerate(questions, 1):
            if show_progress and not self.verbose:
                # 简单进度显示
                print(f"\r[{i}/{len(questions)}] 处理中...", end='', flush=True)
            
            try:
                result = self.predict(question, return_confidence=True, return_raw=False)
                results.append(result)
                total_time += result.get('response_time', 0.0)
                
            except Exception as e:
                if continue_on_error:
                    error_result = {
                        'question': question,
                        'intent': 'ERROR',
                        'confidence': 'N/A',
                        'category_id': -1,
                        'success': False,
                        'error': f'BATCH_ERROR: {str(e)}',
                        'response_time': 0.0
                    }
                    results.append(error_result)
                    if self.verbose:
                        print(f"[WARNING] 问题 {i} 处理失败,继续处理下一个")
                else:
                    if show_progress:
                        print()  # 换行
                    raise
        
        if show_progress and not self.verbose:
            print()  # 换行
        
        # 计算汇总统计
        summary = self._compute_summary(results, total_time)
        
        if self.verbose or show_progress:
            print(f"[INFO] 批量预测完成")
            print(f"  总计: {summary['total']}")
            print(f"  成功: {summary['success']}")
            print(f"  失败: {summary['failed']}")
            print(f"  成功率: {summary['success_rate']:.1f}%")
            print(f"  平均响应时间: {summary['avg_response_time']:.2f}秒")
        
        return {
            'results': results,
            'summary': summary
        }
    
    def get_intent_categories(self) -> List[str]:
        """
        获取所有意图类别
        
        Returns:
            意图类别列表
            
        Example:
            >>> categories = system.get_intent_categories()
            >>> print(categories)
            ['参数标准', '部件检查', '故障处理', ...]
        """
        return self._predictor.INTENT_CATEGORIES.copy()
    
    def set_temperature(self, value: float) -> None:
        """
        动态调整采样温度
        
        Args:
            value: 温度值(0.0-1.0)
            
        Example:
            >>> system.set_temperature(0.15)
        """
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"温度值必须在0.0-1.0之间,当前值: {value}")
        
        self.temperature = value
        self._predictor.temperature = value
        
        if self.verbose:
            print(f"[INFO] 温度已更新为: {value}")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取系统运行统计
        
        Returns:
            {
                'total_predictions': int,      # 总预测次数
                'successful_predictions': int, # 成功预测次数
                'failed_predictions': int,     # 失败预测次数
                'success_rate': float,         # 成功率(%)
                'avg_response_time': float     # 平均响应时间(秒)
            }
            
        Example:
            >>> stats = system.get_stats()
            >>> print(f"总预测次数: {stats['total_predictions']}")
        """
        total = self._stats['total_predictions']
        success_rate = (self._stats['successful_predictions'] / total * 100) if total > 0 else 0.0
        avg_time = (self._stats['total_time'] / total) if total > 0 else 0.0
        
        return {
            'total_predictions': total,
            'successful_predictions': self._stats['successful_predictions'],
            'failed_predictions': self._stats['failed_predictions'],
            'success_rate': round(success_rate, 2),
            'avg_response_time': round(avg_time, 3)
        }
    
    def reset_stats(self) -> None:
        """
        重置统计信息
        
        Example:
            >>> system.reset_stats()
        """
        self._stats = {
            'total_predictions': 0,
            'successful_predictions': 0,
            'failed_predictions': 0,
            'total_time': 0.0
        }
        if self.verbose:
            print("[INFO] 统计信息已重置")
    
    # ==================== 内部方法 ====================
    
    def _format_result(
        self,
        raw_result: Dict,
        return_confidence: bool,
        return_raw: bool
    ) -> Dict[str, Any]:
        """
        格式化预测结果为统一格式
        
        Args:
            raw_result: TrackIntentPredictor返回的原始结果
            return_confidence: 是否返回置信度
            return_raw: 是否返回原始输出
            
        Returns:
            格式化后的结果字典
        """
        # 检查是否有错误
        if 'error' in raw_result:
            return {
                'question': raw_result.get('question', ''),
                'intent': raw_result.get('intent', 'ERROR'),
                'confidence': 'N/A',
                'category_id': -1,
                'success': False,
                'error': raw_result['error']
            }
        
        # 正常结果
        intent = raw_result.get('intent', 'UNKNOWN')
        
        # 确定category_id
        categories = self.get_intent_categories()
        try:
            category_id = categories.index(intent) + 1 if intent in categories else 0
        except ValueError:
            category_id = 0
        
        result = {
            'question': raw_result.get('question', ''),
            'intent': intent,
            'category_id': category_id,
            'success': True
        }
        
        # 添加置信度
        if return_confidence:
            result['confidence'] = raw_result.get('confidence', 'N/A')
        
        # 添加原始输出
        if return_raw:
            result['raw_output'] = raw_result.get('raw_output', '')
        
        return result
    
    def _update_stats(self, response_time: float, success: bool) -> None:
        """
        更新统计信息
        
        Args:
            response_time: 响应时间
            success: 是否成功
        """
        self._stats['total_predictions'] += 1
        self._stats['total_time'] += response_time
        
        if success:
            self._stats['successful_predictions'] += 1
        else:
            self._stats['failed_predictions'] += 1
    
    def _compute_summary(self, results: List[Dict], total_time: float) -> Dict[str, Any]:
        """
        计算批量预测的统计信息
        
        Args:
            results: 预测结果列表
            total_time: 总耗时
            
        Returns:
            汇总统计字典
        """
        total = len(results)
        success_count = sum(1 for r in results if r.get('success', False))
        failed_count = total - success_count
        
        # 计算意图分布
        intent_distribution = {}
        for result in results:
            if result.get('success', False):
                intent = result.get('intent', 'UNKNOWN')
                intent_distribution[intent] = intent_distribution.get(intent, 0) + 1
        
        # 按计数排序
        intent_distribution = dict(sorted(
            intent_distribution.items(),
            key=lambda x: x[1],
            reverse=True
        ))
        
        return {
            'total': total,
            'success': success_count,
            'failed': failed_count,
            'success_rate': round((success_count / total * 100) if total > 0 else 0.0, 2),
            'avg_response_time': round((total_time / total) if total > 0 else 0.0, 3),
            'intent_distribution': intent_distribution
        }


# ==================== 模块级便捷函数 ====================

def quick_predict(question: str, **kwargs) -> str:
    """
    快速预测单个问题的意图(简化版)
    
    Args:
        question: 用户问题
        **kwargs: 传递给QAIntentSystem的参数
        
    Returns:
        预测的意图字符串
        
    Example:
        >>> from intent import quick_predict
        >>> intent = quick_predict("轴承径向游隙的标准范围是多少?")
        >>> print(intent)  # "参数标准"
    """
    system = QAIntentSystem(**kwargs)
    result = system.predict(question)
    return result['intent'] if result['success'] else 'ERROR'


# ==================== 主程序入口 ====================

if __name__ == "__main__":
    # 简单的命令行测试
    print("=" * 70)
    print("QAIntentSystem 测试")
    print("=" * 70)
    
    # 初始化系统
    system = QAIntentSystem(verbose=True)
    
    # 测试单个预测
    print("\n【测试1: 单个预测】")
    test_question = "轴承径向游隙的标准范围是多少?"
    result = system.predict(test_question)
    print(f"\n问题: {result['question']}")
    print(f"意图: {result['intent']}")
    print(f"置信度: {result.get('confidence', 'N/A')}")
    print(f"类别ID: {result['category_id']}")
    print(f"成功: {result['success']}")
    print(f"响应时间: {result['response_time']}秒")
    
    # 测试批量预测
    print("\n【测试2: 批量预测】")
    test_questions = [
        "轴承径向游隙的标准范围是多少?",
        "检查空气干燥器筒时,对上盖和筒体焊缝有何要求?",
        "制动缸体内表面出现拉伤应如何处理?"
    ]
    batch_result = system.predict_batch(test_questions, show_progress=True)
    
    print(f"\n批量预测汇总:")
    for key, value in batch_result['summary'].items():
        print(f"  {key}: {value}")
    
    # 测试统计功能
    print("\n【测试3: 统计信息】")
    stats = system.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    # 测试获取意图类别
    print("\n【测试4: 意图类别】")
    categories = system.get_intent_categories()
    print(f"支持的意图类别({len(categories)}个):")
    for i, cat in enumerate(categories, 1):
        print(f"  {i}. {cat}")
    
    print("\n" + "=" * 70)
    print("测试完成!")
    print("=" * 70)