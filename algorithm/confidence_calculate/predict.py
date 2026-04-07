import sys
import os
from pathlib import Path
import importlib.util
import torch
import json
import numpy as np
import time
import yaml
from dotenv import load_dotenv

# 加载 backend/.env
env_path = Path(__file__).parent.parent.parent / 'backend' / '.env'
if env_path.exists():
    load_dotenv(env_path)

# 获取当前文件所在目录
CURRENT_DIR = Path(__file__).resolve().parent

# 将所有必要的目录添加到 sys.path
required_paths = [
    str(CURRENT_DIR),
    str(CURRENT_DIR / "models"),
    str(CURRENT_DIR / "components"),
]

for path in required_paths:
    if path not in sys.path:
        sys.path.insert(0, path)

# 定义各个模块的绝对路径
base_model_path = CURRENT_DIR / "models" / "base_model.py"
dynamic_hybrid_path = CURRENT_DIR / "models" / "dynamic_hybrid.py"
config_yaml_path = CURRENT_DIR / "config.yaml"
extractor_path = CURRENT_DIR / "utils" / "extractor.py"


def load_module(module_name, file_path):
    """从指定文件路径加载模块"""
    if not file_path.exists():
        print(f"警告: 模块文件不存在: {file_path}")
        return None
    
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        print(f"警告: 无法从 {file_path} 创建模块规范")
        return None
    
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 加载 base_model
base_module = load_module("base_model", base_model_path)
if base_module:
    sys.modules["base_model"] = base_module

# 加载 dynamic_hybrid
dynamic_hybrid_module = load_module("dynamic_hybrid", dynamic_hybrid_path)

# 加载 extractor（如果存在）
if extractor_path.exists():
    extractor_module = load_module("extractor", extractor_path)
else:
    extractor_module = None

# 从加载的模块中获取类
DynamicHybridModel = dynamic_hybrid_module.DynamicHybridModel if dynamic_hybrid_module else None
Extractor = extractor_module if extractor_module else None


class Config:
    """简单的配置类，直接从 YAML 读取"""
    def __init__(self, config_path: str):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
    
    def get(self, key: str, default=None):
        """支持点号分隔的键，如 'text.es_size'"""
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value


class ConfidencePredictor:
    """置信度预测器"""
    
    def __init__(self, model_path: str, config_path: str = None):
        """初始化预测器"""
        if config_path is None:
            config_path = str(CURRENT_DIR / 'config.yaml')
        
        # 检查配置文件是否存在
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
        
        self.config = Config(config_path)
        
        # 自动选择设备
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        print(f"使用设备: {self.device}")
        
        # 检查 DynamicHybridModel 是否可用
        if DynamicHybridModel is None:
            raise ImportError("无法加载 DynamicHybridModel，请检查 models/dynamic_hybrid.py")
        
        # 检查模型文件是否存在
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")
        
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
            'best_path': component_scores.get('best_path', None)
        }
    
    def predict_batch(self, triples: list) -> list:
        """批量预测"""
        results = []
        for triple in triples:
            results.append(self.predict(triple))
        return results
    
    def extract_from_schema_output(self, data: dict, confidence_threshold: float = None) -> dict:
        """
        从 schema_mapper 输出中提取关系并计算置信度
        """
        if confidence_threshold is None:
            confidence_threshold = float(os.getenv('KG_CONFIDENCE_THRESHOLD', '0.7'))
        
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
        try:
            relations_to_evaluate, relation_info = Extractor.extract_relations_from_graph(graph)
        except Exception as e:
            return {
                "success": False,
                "message": f"提取关系失败: {e}",
                "statistics": {"total": 0, "high": 0, "low": 0},
                "relations_high": [],
                "relations_low": [],
                "full_relations": [],
                "predictions": []
            }
        
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
        triples, true_values = self.load_test_data(json_path)
        
        predictions = []
        component_scores_list = []
        best_paths = []
        times = []
        
        print("\n开始批量测试...")
        print("-" * 60)
        
        start_total = time.time()
        
        for i, triple in enumerate(triples):
            start_time = time.time()
            
            with torch.no_grad():
                component_scores = self.model.get_component_scores(triple)
            
            end_time = time.time()
            sample_time = end_time - start_time
            
            predictions.append(component_scores['final_confidence'])
            component_scores_list.append({
                'text': component_scores['text_score'],
                'kg': component_scores['kg_score'],
                'path': component_scores['path_score']
            })
            best_paths.append(component_scores.get('best_path', None))
            times.append(sample_time)
            
            print(f"样本 {i+1:3d}/{len(triples)} | "
                  f"真实: {true_values[i]:.2f} | "
                  f"预测: {component_scores['final_confidence']:.2f} | "
                  f"误差: {abs(component_scores['final_confidence'] - true_values[i]):.2f} | "
                  f"时间: {sample_time*1000:.1f}ms")
        
        end_total = time.time()
        
        print("-" * 60)
        print(f"测试完成! 总耗时: {end_total - start_total:.2f}秒")
        print(f"平均每个样本: {np.mean(times)*1000:.2f}毫秒")
        
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


def get_predictor(model_path: str = None, config_path: str = None):
    """获取预测器实例"""
    if model_path is None:
        model_path = str(CURRENT_DIR / 'final_model.pt')
    if config_path is None:
        config_path = str(CURRENT_DIR / 'config.yaml')
    
    return ConfidencePredictor(model_path, config_path)


if __name__ == '__main__':
    predictor = get_predictor()
    
    test_data_path = CURRENT_DIR / 'data' / 'test.json'
    if test_data_path.exists():
        results = predictor.test_all(str(test_data_path))
    else:
        print(f"测试数据不存在: {test_data_path}")
    
    print("\n" + "="*60)
    print("测试完成！")
    print("="*60)