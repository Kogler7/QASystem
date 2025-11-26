import os

# Ensure OpenAI API key is set via environment variable
if not os.getenv("OPENAI_API_KEY"):
    raise ValueError("OPENAI_API_KEY environment variable is required. Please set it before running this script.")

import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import LabelEncoder
from intentgpt.utils.metrics import clustering_score
from langchain.embeddings import OpenAIEmbeddings
from sentence_transformers import SentenceTransformer

def get_embeddings(model, text_list):
    # Check if model is instance of OpenAIEmbeddings
    if isinstance(model, OpenAIEmbeddings):
        embeddings = model.embed_documents(text_list)
    else:
        embeddings = model.encode(text_list)
    return embeddings

def main():

    model_name = 'text-embedding-ada-002'
    if 'ada' in model_name:
        model = OpenAIEmbeddings()
    elif model == 'sentence-transformer':
        # Use local model
        local_model_path = '../models/all-MiniLM-L6-v2'
        model = SentenceTransformer(os.path.abspath(local_model_path), local_files_only=True)

    DATASETS = {
        "CLINC" : 'results_v3/clinc',
        'TRACK' : 'results_v3/banking'
    }
    
    for dataset in DATASETS:
        for dir in os.listdir(DATASETS[dataset]):
            path_prompt_results = os.path.join(DATASETS[dataset], dir)
            if os.path.exists(os.path.join(path_prompt_results, 'results.csv')):
                results_path = os.path.join(path_prompt_results, 'results.csv')
                df = pd.read_csv(results_path)

                # Compute embeddings
                pred_embeddings = get_embeddings(model, df['intent'].tolist())
                true_embeddings = get_embeddings(model, df['gt_label'].tolist())

                # df['pred_embeddings'] = pred_embeddings.tolist()
                # df['true_embeddings'] = true_embeddings.tolist()
                label_encoder = LabelEncoder()

                y_true = df['gt_label'].tolist()
                labels_true = label_encoder.fit_transform(y_true)

                # Perform kmean clustering
                if dataset == 'CLINC':
                    num_clusters = 150
                elif dataset == 'TRACK':
                    num_clusters = 77
                    
                clustering_model = KMeans(n_clusters=num_clusters)
                clustering_model.fit(pred_embeddings)
                labels_pred = clustering_model.labels_

                clustering_model.fit(true_embeddings)
                labels_true = clustering_model.labels_

                res = clustering_score(labels_true, labels_pred)
                print(f"Dataset: {dataset}, Prompt: {dir}, Clustering score: {res}")



if __name__ == "__main__":
    main()