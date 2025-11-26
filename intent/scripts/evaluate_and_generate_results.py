#!/usr/bin/env python3
"""
独立评估脚本 - 生成缺失的results.csv和scores.csv文件

用法:
    python scripts/evaluate_and_generate_results.py --results_dir results_v3/track/xxx
    
功能:
    1. 检查results.csv和scores.csv是否存在
    2. 如果缺失,尝试从其他文件重建
    3. 如果需要,重新运行评估
    4. 生成详细的诊断报告
"""

import os
import sys
import argparse
import pandas as pd
from pathlib import Path

# 添加项目路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from intentgpt.utils.metrics import clustering_score
from sklearn.preprocessing import LabelEncoder


def check_files(results_dir):
    """检查必要文件是否存在"""
    results_path = Path(results_dir)
    
    files_status = {
        'config.yaml': results_path / 'config.yaml',
        'discovered_categories.txt': results_path / 'discovered_categories.txt',
        'gt_intents.txt': results_path / 'gt_intents.txt',
        'results.csv': results_path / 'results.csv',
        'scores.csv': results_path / 'scores.csv',
        'failed_batches.json': results_path / 'failed_batches.json'
    }
    
    print(f"\n{'='*60}")
    print(f" Checking files in: {results_dir}")
    print(f"{'='*60}\n")
    
    missing_files = []
    for name, path in files_status.items():
        exists = path.exists()
        status = "" if exists else ""
        print(f"{status} {name}: {'Found' if exists else 'Missing'}")
        if not exists and name not in ['failed_batches.json']:  # failed_batches is optional
            missing_files.append(name)
    
    return files_status, missing_files


def load_existing_results(results_csv_path):
    """从已有的results.csv加载数据"""
    try:
        df = pd.read_csv(results_csv_path)
        
        required_cols = ['id', 'utterance', 'intent', 'gt_label']
        missing_cols = [col for col in required_cols if col not in df.columns]
        
        if missing_cols:
            print(f" Missing required columns in results.csv: {missing_cols}")
            return None
        
        print(f" Successfully loaded {len(df)} samples from results.csv")
        return df
        
    except Exception as e:
        print(f" Error loading results.csv: {str(e)}")
        return None


def compute_and_save_scores(df, results_dir):
    """计算评估指标并保存到scores.csv"""
    try:
        print(f"\n{'='*60}")
        print(f" Computing Evaluation Metrics")
        print(f"{'='*60}\n")
        
        y_true = df['gt_label'].tolist()
        y_pred = df['intent'].tolist()
        
        print(f"Number of samples: {len(y_pred)}")
        print(f"Unique true intents: {len(set(y_true))}")
        print(f"Unique predicted intents: {len(set(y_pred))}")
        
        # Simple clustering using label encoding
        label_encoder = LabelEncoder()
        labels_true = label_encoder.fit_transform(y_true)
        labels_pred = label_encoder.fit_transform(y_pred)
        
        # Compute scores
        res = clustering_score(labels_true, labels_pred)
        
        # Add metadata
        from datetime import datetime
        res['date'] = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        res['num_discovered_intents'] = len(set(y_pred))
        res['num_true_intents'] = len(set(y_true))
        res['num_samples'] = len(y_pred)
        res['results_path'] = str(results_dir)
        
        # Save to scores.csv
        scores_df = pd.DataFrame([res])
        scores_csv_path = Path(results_dir) / 'scores.csv'
        scores_df.to_csv(scores_csv_path, index=False)
        
        print(f"\n scores.csv saved to: {scores_csv_path}")
        
        # Print results
        print(f"\n{'='*60}")
        print(f" Evaluation Results")
        print(f"{'='*60}")
        print(f"NMI Score:           {res['NMI']:.2f}%")
        print(f"ARI Score:           {res['ARI']:.2f}%")
        print(f"ACC Score:           {res['ACC']:.2f}%")
        print(f"Discovered Intents:  {res['num_discovered_intents']}")
        print(f"True Intents:        {res['num_true_intents']}")
        print(f"Total Samples:       {res['num_samples']}")
        print(f"{'='*60}\n")
        
        # Performance assessment
        print(" Performance Assessment:")
        if res['NMI'] > 75 and res['ARI'] > 70 and res['ACC'] > 75:
            print("    EXCELLENT - High-quality intent discovery!")
        elif res['NMI'] > 60 and res['ARI'] > 55 and res['ACC'] > 60:
            print("    GOOD - Acceptable intent discovery performance")
        else:
            print("     NEEDS IMPROVEMENT - Consider adjusting:")
            print("      - Temperature parameter (try 0.1-0.2)")
            print("      - Few-shot examples (increase quantity or improve quality)")
            print("      - Prompt design (try hard_prompt strategy)")
        print()
        
        return res
        
    except Exception as e:
        print(f" Error computing scores: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def analyze_errors(df):
    """分析错误预测"""
    errors = df[df['intent'] != df['gt_label']]
    
    print(f"\n{'='*60}")
    print(f"[分析] Error Analysis")
    print(f"{'='*60}\n")
    print(f"Total errors: {len(errors)} / {len(df)} ({len(errors)/len(df)*100:.1f}%)")
    
    if len(errors) > 0:
        print(f"\nSample errors (first 10):")
        print(errors[['utterance', 'intent', 'gt_label']].head(10).to_string(index=False))
        
        # Save full error report
        error_report_path = Path(df.attrs.get('results_dir', '.')) / 'error_analysis.csv'
        errors.to_csv(error_report_path, index=False)
        print(f"\n Full error report saved to: {error_report_path}")


def main():
    parser = argparse.ArgumentParser(description='Evaluate IntentGPT results and generate missing CSV files')
    parser.add_argument('--results_dir', type=str, required=True,
                        help='Path to results directory (e.g., results_v3/track/xxx)')
    parser.add_argument('--analyze_errors', action='store_true',
                        help='Perform detailed error analysis')
    
    args = parser.parse_args()
    
    # Check if directory exists
    if not os.path.exists(args.results_dir):
        print(f" Error: Directory not found: {args.results_dir}")
        return 1
    
    # Check file status
    files_status, missing_files = check_files(args.results_dir)
    
    # Handle missing results.csv
    if 'results.csv' in missing_files:
        print(f"\n results.csv is missing!")
        print(f"  Cannot generate scores without results.csv")
        print(f" You need to re-run the experiment to generate results.csv")
        return 1
    
    # Load results.csv
    df = load_existing_results(files_status['results.csv'])
    if df is None:
        return 1
    
    df.attrs['results_dir'] = args.results_dir
    
    # Generate scores.csv if missing
    if 'scores.csv' in missing_files:
        print(f"\n scores.csv is missing, generating...")
        scores = compute_and_save_scores(df, args.results_dir)
        if scores is None:
            return 1
    else:
        print(f"\n scores.csv already exists")
        # Still print the scores
        compute_and_save_scores(df, args.results_dir)
    
    # Error analysis
    if args.analyze_errors:
        analyze_errors(df)
    
    print(f"\n{'='*60}")
    print(f" Evaluation Complete!")
    print(f"{'='*60}\n")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())