import sys
import os
from pathlib import Path
import importlib.util
import torch
import json
import numpy as np
import time

# 获取当前文件所在目录
CURRENT_DIR = Path(__file__).resolve().parent

# 将所有必要的目录添加到 sys.path，以便包导入能工作
required_paths = [
    str(CURRENT_DIR),                          # 根目录
    str(CURRENT_DIR / "models"),                # models目录
    str(CURRENT_DIR / "utils"),                 # utils目录
    str(CURRENT_DIR / "components"),            # components目录
]

for path in required_paths:
    if path not in sys.path:
        sys.path.insert(0, path)

# 定义各个模块的绝对路径
base_model_path = CURRENT_DIR / "models" / "base_model.py"
dynamic_hybrid_path = CURRENT_DIR / "models" / "dynamic_hybrid.py"
config_path = CURRENT_DIR / "utils" / "config.py"
extractor_path = CURRENT_DIR / "utils" / "extractor.py"

def load_module(module_name, file_path):
    """从指定文件路径加载模块"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# 先加载 base_model 并注册到 sys.modules，供 dynamic_hybrid 内部导入使用
base_module = load_module("base_model", base_model_path)
sys.modules["base_model"] = base_module

# 加载 dynamic_hybrid
dynamic_hybrid_module = load_module("dynamic_hybrid", dynamic_hybrid_path)
# 加载 config
config_module = load_module("config", config_path)
# 加载 extractor（如果存在）
if extractor_path.exists():
    extractor_module = load_module("extractor", extractor_path)
else:
    extractor_module = None

# 从加载的模块中获取类
DynamicHybridModel = dynamic_hybrid_module.DynamicHybridModel
Config = config_module.Config
Extractor = extractor_module if extractor_module else None


class ConfidencePredictor:
    """置信度预测器"""
    
    def __init__(self, model_path: str, config_path: str = 'config.yaml'):
        """初始化预测器"""
        self.config = Config(config_path)
        
        # 自动选择设备
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print(f"使用设备: {self.device}")
        
        # 加载模型
        print("加载模型中...")
        try:
            self.model = DynamicHybridModel(self.config)
            state = torch.load(model_path, map_location=self.device, weights_only=False)
            if isinstance(state, dict) and "state_dict" in state:
                state = state["state_dict"]
            self.model.load_state_dict(state)
        except Exception as e:
            err_msg = str(e).lower()
            if ("deserializ" in err_msg or "incomplete metadata" in err_msg or "file not fully covered" in err_msg
                    or "pytorchstreamreader" in err_msg or "central directory" in err_msg or "zip archive" in err_msg):
                raise RuntimeError(
                    f"模型文件不完整或损坏: {model_path}（{e}）。请重新训练置信度模型并生成 final_model.pt。"
                ) from e
            raise
        self.model = self.model.to(self.device)
        self.model.eval()
        print("模型加载完成")
    
    def predict(self, triple: dict) -> dict:
        """预测单个三元组的置信度"""
        with torch.no_grad():
            component_scores = self.model.get_component_scores(triple)
        
        return {
            'confidence': round(float(component_scores['final_confidence']), 2),
            'component_scores': {
                'text': round(float(component_scores['text_score']), 2),
                'kg': round(float(component_scores['kg_score']), 2),
                'path': round(float(component_scores['path_score']), 2)
            },
            'best_path': component_scores['best_path']
        }
    
    def predict_batch(self, triples: list) -> list:
        """批量预测"""
        results = []
        for triple in triples:
            results.append(self.predict(triple))
        return results
    
    def extract_from_schema_output(self, data: dict, confidence_threshold: float = 0.7) -> dict:
        """
        从 schema_mapper 输出中提取关系并计算置信度
        
        Args:
            data: schema_mapper 输出的 JSON（包含 raw 和 graph）
            confidence_threshold: 置信度阈值
        
        Returns:
            包含计算结果的字典
        """
        if Extractor is None:
            return {
                "success": False,
                "message": "extractor 模块未加载，请检查 utils/extractor.py 是否存在",
                "statistics": {"total": 0, "high": 0, "low": 0},
                "relations_high": [],
                "relations_low": [],
                "full_relations": [],
                "predictions": []
            }
        
        graph = data.get('graph', {})
        
        # 使用 extractor 提取关系
        relations_to_evaluate, relation_info = Extractor.extract_relations_from_graph(graph)
        
        if not relations_to_evaluate:
            return {
                "success": True,
                "message": "未发现任何关系",
                "statistics": {"total": 0, "high": 0, "low": 0},
                "relations_high": [],
                "relations_low": [],
                "full_relations": [],
                "predictions": []
            }
        
        # 计算置信度
        predictions = self.predict_batch(relations_to_evaluate)
        
        # 构建完整预测结果
        full_predictions = []
        high_confidence_relations = []
        low_confidence_relations = []
        
        for i, (pred, info) in enumerate(zip(predictions, relation_info)):
            confidence = pred['confidence']
            
            display_triple = {
                'subject': info['subject'],
                'predicate': info['predicate'],
                'object': info['object'],
                'confidence': confidence
            }
            
            full_predictions.append({
                'subject': info['subject'],
                'predicate': info['predicate'],
                'object': info['object'],
                'confidence': confidence,
                'component_scores': pred['component_scores']
            })
            
            if confidence >= confidence_threshold:
                high_confidence_relations.append(display_triple)
            else:
                low_confidence_relations.append(display_triple)
        
        stats = {
            "total": len(relations_to_evaluate),
            "high": len(high_confidence_relations),
            "low": len(low_confidence_relations)
        }
        
        return {
            "success": True,
            "message": f"计算完成: 总计{stats['total']}个关系, 高置信度(≥{confidence_threshold}){stats['high']}个, 低置信度{stats['low']}个",
            "statistics": stats,
            "relations_high": high_confidence_relations,
            "relations_low": low_confidence_relations,
            "full_relations": relation_info,
            "predictions": full_predictions
        }
    
    def load_test_data(self, json_path: str):
        """加载测试数据"""
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        triples = [item['triple'] for item in data]
        true_values = [item['label'] for item in data]
        
        print(f"加载测试集: {len(triples)} 个样本")
        return triples, true_values
    
    def test_all(self, json_path: str):
        """测试所有样本"""
        # 加载数据
        triples, true_values = self.load_test_data(json_path)
        
        # 存储结果
        predictions = []
        component_scores_list = []
        best_paths = []
        times = []
        
        print("\n开始批量测试...")
        print("-" * 60)
        
        # 逐个测试所有样本
        start_total = time.time()
        
        for i, triple in enumerate(triples):
            start_time = time.time()
            
            with torch.no_grad():
                component_scores = self.model.get_component_scores(triple)
            
            end_time = time.time()
            sample_time = end_time - start_time
            
            # 保存结果
            predictions.append(component_scores['final_confidence'])
            component_scores_list.append({
                'text': component_scores['text_score'],
                'kg': component_scores['kg_score'],
                'path': component_scores['path_score']
            })
            best_paths.append(component_scores['best_path'])
            times.append(sample_time)
            
            # 打印每个样本的结果
            print(f"样本 {i+1:3d}/{len(triples)} | "
                  f"真实: {true_values[i]:.2f} | "
                  f"预测: {component_scores['final_confidence']:.2f} | "
                  f"误差: {abs(component_scores['final_confidence'] - true_values[i]):.2f} | "
                  f"时间: {sample_time*1000:.1f}ms")
        
        end_total = time.time()
        
        print("-" * 60)
        print(f"测试完成! 总耗时: {end_total - start_total:.2f}秒")
        print(f"平均每个样本: {np.mean(times)*1000:.2f}毫秒")
        
        # 保存详细结果到文件
        self.save_results(
            triples, true_values, predictions, 
            component_scores_list, best_paths,
            'test_results.json'
        )
        
        return {
            'total_time': end_total - start_total,
            'avg_time': np.mean(times),
            'predictions': predictions,
            'true_values': true_values
        }
    
    def save_results(self, triples, true_values, predictions, 
                     component_scores, best_paths, filename):
        """保存详细结果到文件"""
        results = []
        for i in range(len(triples)):
            results.append({
                'index': i + 1,
                'triple': triples[i],
                'true_value': round(true_values[i], 2),
                'predicted_value': round(predictions[i], 2),
                'error': round(abs(predictions[i] - true_values[i]), 2),
                'component_scores': {
                    'text': round(component_scores[i]['text'], 2),
                    'kg': round(component_scores[i]['kg'], 2),
                    'path': round(component_scores[i]['path'], 2)
                },
                'best_path': best_paths[i] if best_paths[i] else None
            })
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n详细结果已保存到 {filename}")


if __name__ == '__main__':
    # 创建预测器
    predictor = ConfidencePredictor('final_model.pt')
    
    # 测试所有样本
    results = predictor.test_all('data/test.json')
    
    print("\n" + "="*60)
    print("测试完成！")
    print("="*60)