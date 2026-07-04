import pandas as pd
import os
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
import numpy as np

# Load data
path = '/path/to/results.csv'
df = pd.read_csv(path)  # Replace 'your_file.csv' with your file path

# Initialize SentenceBERT with local model
local_model_path = '../models/all-MiniLM-L6-v2'
model = SentenceTransformer(os.path.abspath(local_model_path), local_files_only=True)

# Compute embeddings
discovered_embeddings = model.encode(df['intent'].tolist())
ground_truth_embeddings = model.encode(df['gt_label'].tolist())

# Compute syntactic similarities
similarities = cosine_similarity(discovered_embeddings, ground_truth_embeddings)

# t-SNE for visualization
embeddings = np.vstack((discovered_embeddings, ground_truth_embeddings))
tsne = TSNE(n_components=2, random_state=0)
tsne_results = tsne.fit_transform(embeddings)

# Plotting
fig, ax = plt.subplots()
ax.scatter(tsne_results[:len(discovered_embeddings), 0], tsne_results[:len(discovered_embeddings), 1], c='deepskyblue', marker='o', label='Discovered Intents')
ax.scatter(tsne_results[len(discovered_embeddings):, 0], tsne_results[len(discovered_embeddings):, 1], c='coral', marker='x', label='Ground Truth Intents')
ax.legend()
plt.savefig('tsne.png')
plt.close()


