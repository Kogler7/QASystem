#!/usr/bin/env python3
"""
QAIntentSystem 使用示例

演示如何使用封装好的接口进行意图识别。
运行前请确保vLLM服务已启动。
"""

from intent import QAIntentSystem, quick_predict


def example_1_basic_usage():
    """示例1: 基础使用"""
    print("=" * 70)
    print("示例1: 基础使用")
    print("=" * 70)
    
    # 初始化系统
    system = QAIntentSystem(verbose=True)
    
    # 预测单个问题
    question = "轴承径向游隙的标准范围是多少?"
    result = system.predict(question)
    
    if result['success']:
        print(f"\n✓ 预测成功!")
        print(f"  问题: {result['question']}")
        print(f"  意图: {result['intent']}")
        print(f"  置信度: {result['confidence']}")
        print(f"  类别ID: {result['category_id']}")
        print(f"  响应时间: {result['response_time']}秒")
    else:
        print(f"\n✗ 预测失败: {result['error']}")
    
    print()


def example_2_batch_processing():
    """示例2: 批量处理"""
    print("=" * 70)
    print("示例2: 批量处理")
    print("=" * 70)
    
    system = QAIntentSystem()
    
    # 准备测试问题
    questions = [
        "轴承径向游隙的标准范围是多少?",
        "检查空气干燥器筒时,对上盖和筒体焊缝有何要求?",
        "制动缸体内表面出现拉伤应如何处理?",
        "柴油机主要紧固件的紧固力矩应遵循哪个表格的规定?",
        "绝缘介电试验合格的标准是什么?",
        "柴油机的工作原理是什么?",
        "端盖轴承孔的圆柱度中修限度与禁用限度有何区别?",
        "万向轴组装时两端叉头安装十字轴孔的中心线应在什么位置?"
    ]
    
    # 批量预测
    result = system.predict_batch(questions, show_progress=True)
    
    # 显示汇总信息
    print(f"\n批量预测汇总:")
    print(f"  总计: {result['summary']['total']}")
    print(f"  成功: {result['summary']['success']}")
    print(f"  失败: {result['summary']['failed']}")
    print(f"  成功率: {result['summary']['success_rate']:.1f}%")
    print(f"  平均响应时间: {result['summary']['avg_response_time']:.2f}秒")
    
    # 显示意图分布
    print(f"\n意图分布:")
    for intent, count in result['summary']['intent_distribution'].items():
        print(f"  {intent}: {count}")
    
    # 显示详细结果
    print(f"\n详细结果:")
    for i, item in enumerate(result['results'], 1):
        if item['success']:
            print(f"  {i}. [{item['intent']}] {item['question'][:40]}...")
        else:
            print(f"  {i}. [ERROR] {item['question'][:40]}...")
    
    print()


def example_3_error_handling():
    """示例3: 错误处理"""
    print("=" * 70)
    print("示例3: 错误处理")
    print("=" * 70)
    
    system = QAIntentSystem()
    
    # 测试空输入
    print("\n测试1: 空输入")
    result = system.predict("")
    print(f"结果: {result['intent']}, 错误: {result.get('error', 'N/A')}")
    
    # 测试正常输入
    print("\n测试2: 正常输入")
    result = system.predict("轴承径向游隙的标准范围是多少?")
    if result['success']:
        print(f"结果: {result['intent']}, 置信度: {result['confidence']}")
    else:
        print(f"错误: {result['error']}")
    
    print()


def example_4_quick_predict():
    """示例4: 快速预测"""
    print("=" * 70)
    print("示例4: 快速预测(便捷函数)")
    print("=" * 70)
    
    # 使用便捷函数
    intent = quick_predict("轴承径向游隙的标准范围是多少?")
    print(f"\n问题: 轴承径向游隙的标准范围是多少?")
    print(f"意图: {intent}")
    
    print()


def example_5_advanced_features():
    """示例5: 高级功能"""
    print("=" * 70)
    print("示例5: 高级功能")
    print("=" * 70)
    
    system = QAIntentSystem()
    
    # 获取意图类别
    print("\n1. 获取意图类别:")
    categories = system.get_intent_categories()
    print(f"支持的意图类别({len(categories)}个):")
    for i, cat in enumerate(categories, 1):
        print(f"  {i}. {cat}")
    
    # 调整温度
    print("\n2. 调整温度参数:")
    print(f"当前温度: {system.temperature}")
    system.set_temperature(0.15)
    print(f"新温度: {system.temperature}")
    
    # 获取统计信息
    print("\n3. 获取统计信息:")
    # 先进行几次预测
    system.predict("轴承径向游隙的标准范围是多少?")
    system.predict("检查空气干燥器筒时有何要求?")
    
    stats = system.get_stats()
    print(f"总预测次数: {stats['total_predictions']}")
    print(f"成功预测: {stats['successful_predictions']}")
    print(f"失败预测: {stats['failed_predictions']}")
    print(f"成功率: {stats['success_rate']:.1f}%")
    print(f"平均响应时间: {stats['avg_response_time']:.2f}秒")
    
    print()


def example_6_integration():
    """示例6: 集成到应用"""
    print("=" * 70)
    print("示例6: 集成到应用示例")
    print("=" * 70)
    
    class QuestionRouter:
        """问题路由器 - 根据意图分发到不同模块"""
        
        def __init__(self):
            self.qa_system = QAIntentSystem()
            self.handlers = {
                '参数标准': self.handle_parameter,
                '部件检查': self.handle_inspection,
                '故障处理': self.handle_troubleshooting,
                '操作规程': self.handle_procedure,
                '试验要求': self.handle_testing,
                '通用知识': self.handle_knowledge,
                '维修限度': self.handle_maintenance,
                '安装要求': self.handle_installation
            }
        
        def route_question(self, question: str):
            """路由问题到对应处理器"""
            result = self.qa_system.predict(question)
            
            if not result['success']:
                return f"处理失败: {result['error']}"
            
            intent = result['intent']
            handler = self.handlers.get(intent, self.handle_unknown)
            
            return handler(question, result)
        
        def handle_parameter(self, question, result):
            return f"[参数标准模块] 查询数据库: {question[:30]}..."
        
        def handle_inspection(self, question, result):
            return f"[部件检查模块] 检索检查规范: {question[:30]}..."
        
        def handle_troubleshooting(self, question, result):
            return f"[故障处理模块] 诊断问题: {question[:30]}..."
        
        def handle_procedure(self, question, result):
            return f"[操作规程模块] 查找流程: {question[:30]}..."
        
        def handle_testing(self, question, result):
            return f"[试验要求模块] 检索标准: {question[:30]}..."
        
        def handle_knowledge(self, question, result):
            return f"[通用知识模块] 解释原理: {question[:30]}..."
        
        def handle_maintenance(self, question, result):
            return f"[维修限度模块] 查询限值: {question[:30]}..."
        
        def handle_installation(self, question, result):
            return f"[安装要求模块] 查找规范: {question[:30]}..."
        
        def handle_unknown(self, question, result):
            return f"[未知] 无法处理的问题: {question[:30]}..."
    
    # 使用路由器
    router = QuestionRouter()
    
    test_questions = [
        "轴承径向游隙的标准范围是多少?",
        "制动缸体内表面拉伤如何处理?",
        "柴油机的工作原理是什么?"
    ]
    
    print("\n问题路由演示:")
    for question in test_questions:
        response = router.route_question(question)
        print(f"\n问题: {question}")
        print(f"响应: {response}")
    
    print()


def main():
    """主函数"""
    print("\n" + "=" * 70)
    print("QAIntentSystem 完整使用示例")
    print("=" * 70)
    print("\n请确保vLLM服务已启动:")
    print("  python -m vllm.entrypoints.api_server --model Qwen/Qwen3-8B\n")
    
    try:
        # 运行所有示例
        example_1_basic_usage()
        input("按Enter继续下一个示例...")
        
        example_2_batch_processing()
        input("按Enter继续下一个示例...")
        
        example_3_error_handling()
        input("按Enter继续下一个示例...")
        
        example_4_quick_predict()
        input("按Enter继续下一个示例...")
        
        example_5_advanced_features()
        input("按Enter继续下一个示例...")
        
        example_6_integration()
        
        print("\n" + "=" * 70)
        print("所有示例运行完成!")
        print("=" * 70)
        
    except KeyboardInterrupt:
        print("\n\n用户中断,退出示例程序")
    except Exception as e:
        print(f"\n运行出错: {str(e)}")
        print("\n请检查:")
        print("  1. vLLM服务是否正常运行")
        print("  2. API地址配置是否正确")
        print("  3. 网络连接是否正常")


if __name__ == "__main__":
    main()