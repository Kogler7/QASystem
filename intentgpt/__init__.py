"""
IntentGPT: Few-Shot Intent Discovery with Large Language Models

A training-free framework for intent discovery and classification using LLMs.
"""

__version__ = "1.0.0"
__author__ = "Juan A. Rodriguez et al."

from .core.intent_gpt import IntentGPT
from .core.embeddings import Embedder, EmbeddingDB

__all__ = ["IntentGPT", "Embedder", "EmbeddingDB"]
