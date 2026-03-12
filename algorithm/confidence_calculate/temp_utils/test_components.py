import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import json
import numpy as np
from components.text_evidence import TextEvidence
from components.kge_evidence import KGEEvidence
from components.path_evidence import PathEvidence
from utils.config import Config


def test_text_evidence(config):
    print("\n" + "="*60)
    print("1. 测试文本证据组件 (TextEvidence)")
    print("="*60)
    
    text_evidence = TextEvidence(config.get('text', {}))
    text_evidence.eval()
    
    test_triples = [
        {
            "subject": "铁道车辆",
            "predicate": "定义",
            "object": "在铁路轨道上用于运送旅客、货物和为此服务的单元载运工具"
        },
        {
            "subject": "高速客车",
            "predicate": "定义",
            "object": "指最高运行速度大于200km/h的客车"
        }
    ]
    
    print("\n测试用例:")
    for i, triple in enumerate(test_triples):
        print(f"  [{i+1}] {triple['subject']} - {triple['predicate']} - {triple['object'][:30]}...")
    
    print("\n测试结果:")
    for triple in test_triples:
        features = text_evidence.get_text_features(triple)
        print(f"\n  >> {triple['subject']} 的文本特征:")
        print(f"     特征维度: {features.shape}")
        print(f"     特征统计: 均值={np.mean(features):.4f}, 标准差={np.std(features):.4f}")
        print(f"     特征范围: [{np.min(features):.4f}, {np.max(features):.4f}]")
        
        with torch.no_grad():
            processed = text_evidence(triple)
            print(f"     处理后维度: {processed.shape}")
            print(f"     处理后均值: {processed.mean().item():.4f}")
        
        confidence = text_evidence._compute_text_confidence(processed)
        print(f"     文本置信度: {confidence:.4f}")
    
    return text_evidence


def test_kge_evidence(config):
    print("\n" + "="*60)
    print("2. 测试KGE证据组件 (KGEEvidence)")
    print("="*60)
    
    kge_evidence = KGEEvidence(config.get('kg', {}))
    kge_evidence.eval()
    
    test_triples = [
        {
            "subject": "铁道车辆",
            "predicate": "定义",
            "object": "在铁路轨道上用于运送旅客、货物和为此服务的单元载运工具"
        },
        {
            "subject": "客车",
            "predicate": "定义",
            "object": "供运送旅客和为此服务的或原则上编组在旅客列车中使用的车辆"
        }
    ]
    
    print("\n测试用例:")
    for i, triple in enumerate(test_triples):
        print(f"  [{i+1}] {triple['subject']} - {triple['predicate']} - {triple['object'][:30]}...")
    
    print("\n测试结果:")
    for triple in test_triples:
        with torch.no_grad():
            features = kge_evidence(triple)
            print(f"\n  >> {triple['subject']} 的KGE特征:")
            print(f"     特征维度: {features.shape}")
            print(f"     特征统计: 均值={features.mean().item():.4f}, 标准差={features.std().item():.4f}")
        
        confidence = kge_evidence.get_confidence_score(triple)
        print(f"     KGE置信度: {confidence:.4f}")
        
        print(f"     实体嵌入检查:")
        s_emb = kge_evidence._lookup_entity(triple['subject'])
        o_emb = kge_evidence._lookup_entity(triple['object'])
        p_emb = kge_evidence._lookup_relation(triple['predicate'])
        print(f"       主语嵌入维度: {s_emb.shape}")
        print(f"       谓语嵌入维度: {p_emb.shape}")
        print(f"       宾语嵌入维度: {o_emb.shape}")
    
    return kge_evidence


def test_path_evidence(config):
    print("\n" + "="*60)
    print("3. 测试路径证据组件 (PathEvidence with Neo4j)")
    print("="*60)
    
    path_config = {
        'max_path_length': config.get('path.max_path_length', 3),
        'max_paths_per_triple': config.get('path.max_paths_per_triple', 20),
        'path_embedding_dim': config.get('path.path_embedding_dim', 64),
        'neo4j': config.get('path.neo4j', {})
    }
    
    path_evidence = PathEvidence(path_config)
    path_evidence.eval()
    
    test_triples = [
        {
            "subject": "铁道车辆",
            "predicate": "定义",
            "object": "在铁路轨道上用于运送旅客、货物和为此服务的单元载运工具"
        },
        {
            "subject": "客车",
            "predicate": "定义",
            "object": "供运送旅客和为此服务的或原则上编组在旅客列车中使用的车辆"
        }
    ]
    
    print("\n测试用例:")
    for i, triple in enumerate(test_triples):
        print(f"  [{i+1}] {triple['subject']} - {triple['predicate']} - {triple['object'][:30]}...")
    
    print("\n测试结果:")
    for triple in test_triples:
        details = path_evidence.get_path_details(triple)
        
        print(f"\n  >> {triple['subject']} -> {triple['object']}")
        print(f"     找到路径数: {details.get('num_paths', 0)}")
        print(f"     聚合分数: {details.get('aggregate_score', 0):.4f}")
        
        if 'paths' in details:
            for path_info in details['paths']:
                print(f"       路径 {path_info['index']+1}: {path_info['path'][:80]}...")
                print(f"         分数: {path_info['score']:.4f}")
        
        with torch.no_grad():
            result = path_evidence(triple)
            print(f"     路径特征维度: {result['path_features'].shape}")
            print(f"     路径特征均值: {result['path_features'].mean().item():.4f}")
    
    path_evidence.close()
    return path_evidence


def test_integration(config):
    print("\n" + "="*60)
    print("4. 测试组件集成效果")
    print("="*60)
    
    text_evidence = TextEvidence(config.get('text', {}))
    text_evidence.eval()
    
    kge_evidence = KGEEvidence(config.get('kg', {}))
    kge_evidence.eval()
    
    path_config = {
        'max_path_length': config.get('path.max_path_length', 3),
        'max_paths_per_triple': config.get('path.max_paths_per_triple', 20),
        'path_embedding_dim': config.get('path.path_embedding_dim', 64),
        'neo4j': config.get('path.neo4j', {})
    }
    path_evidence = PathEvidence(path_config)
    path_evidence.eval()
    
    test_triple = {
        "subject": "铁道车辆",
        "predicate": "定义",
        "object": "在铁路轨道上用于运送旅客、货物和为此服务的单元载运工具"
    }
    
    print(f"\n集成测试用例:")
    print(f"   {test_triple['subject']} - {test_triple['predicate']} - {test_triple['object'][:50]}...")
    
    print("\n各组件输出:")
    
    with torch.no_grad():
        text_features = text_evidence(test_triple)
        text_conf = text_evidence._compute_text_confidence(text_features)
        print(f"\n  文本组件:")
        print(f"     特征维度: {text_features.shape}")
        print(f"     置信度: {text_conf:.4f}")
    
    with torch.no_grad():
        kge_features = kge_evidence(test_triple)
        kge_conf = kge_evidence.get_confidence_score(test_triple)
        print(f"\n  KGE组件:")
        print(f"     特征维度: {kge_features.shape}")
        print(f"     置信度: {kge_conf:.4f}")
    
    with torch.no_grad():
        path_result = path_evidence(test_triple)
        print(f"\n  路径组件:")
        print(f"     特征维度: {path_result['path_features'].shape}")
        print(f"     找到路径数: {path_result['num_paths']}")
        print(f"     聚合分数: {path_result['aggregate_score']:.4f}")
    
    print("\n  简单融合:")
    avg_confidence = (text_conf + kge_conf + path_result['aggregate_score']) / 3
    print(f"     平均置信度: {avg_confidence:.4f}")
    
    weights = [0.4, 0.3, 0.3]
    weighted_conf = (text_conf * weights[0] + 
                    kge_conf * weights[1] + 
                    path_result['aggregate_score'] * weights[2])
    print(f"     加权置信度: {weighted_conf:.4f} (文本0.4, KGE0.3, 路径0.3)")
    
    path_evidence.close()


def load_config():
    try:
        config = Config('config.yaml')
        print("配置文件加载成功")
        return config
    except Exception as e:
        print(f"配置文件加载失败: {e}")
        print("使用默认配置...")
        return Config()


if __name__ == "__main__":
    print("\n" + "*"*60)
    print("   组件功能测试")
    print("*"*60)
    
    config = load_config()
    
    test_text_evidence(config)
    test_kge_evidence(config)
    test_path_evidence(config)
    test_integration(config)
    
    print("\n" + "*"*60)
    print("   测试完成！")
    print("*"*60)