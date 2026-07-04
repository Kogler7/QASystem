"""Embedding utility functions."""

import os
from sentence_transformers import SentenceTransformer


def get_embedder(embedder_name):
    """Get the appropriate embedder based on the name."""
    if embedder_name == 'sentence-transformer':
        # Always use local model path
        model_path = 'models/all-MiniLM-L6-v2'
        abs_path = os.path.abspath(model_path)
        
        if not os.path.exists(abs_path):
            raise FileNotFoundError(
                f"Local sentence-transformer model not found at: {abs_path}\n"
                f"Please ensure the model is downloaded to this location."
            )
        
        print(f"Loading sentence-transformer from local path: {abs_path}")
        model = SentenceTransformer(abs_path, local_files_only=True)
    else:
        raise ValueError(f"Unsupported embedder: {embedder_name}")
    
    return model
