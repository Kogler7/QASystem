"""Local LLM utilities for loading and using local Llama models."""

import os
import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    pipeline
)
from langchain_core.language_models.llms import LLM
from typing import Optional, List, Any
from pydantic import Field


class LocalLlamaLLM(LLM):
    """Wrapper for local Llama model to work with LangChain."""
    
    model_path: str = Field(...)
    pipeline: Any = Field(default=None)
    temperature: float = Field(default=0.15)
    max_new_tokens: int = Field(default=1000)
    
    class Config:
        arbitrary_types_allowed = True
    
    def __init__(self, model_path: str, temperature: float = 0.15, max_new_tokens: int = 1000, **kwargs):
        """Initialize the local Llama model."""
        super().__init__(model_path=model_path, temperature=temperature, max_new_tokens=max_new_tokens, **kwargs)
        self._load_model()
    
    def _load_model(self):
        """Load the local Llama model with 4-bit quantization."""
        print(f"Loading local Llama model from {self.model_path}...")
        
        # Configure 4-bit quantization
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.float16
        )
        
        # Load tokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            self.model_path,
            local_files_only=True,
            trust_remote_code=True
        )
        
        # Load model
        model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            quantization_config=bnb_config,
            device_map="auto",
            local_files_only=True,
            trust_remote_code=True
        )
        
        # Create pipeline
        self.pipeline = pipeline(
            "text-generation",
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=self.max_new_tokens,
            temperature=self.temperature,
            do_sample=True if self.temperature > 0 else False,
            top_p=0.9 if self.temperature > 0 else 1.0,
            return_full_text=False
        )
        
        print("Model loaded successfully!")
    
    @property
    def _llm_type(self) -> str:
        """Return type of LLM."""
        return "local_llama"
    
    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        **kwargs
    ) -> str:
        """Call the local Llama model."""
        # Format prompt for Llama-3-Instruct
        formatted_prompt = f"<|begin_of_text|><|start_header_id|>user<|end_header_id|>\n\n{prompt}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
        
        # Generate response
        response = self.pipeline(formatted_prompt)[0]['generated_text']
        
        return response.strip()


def load_local_llama(model_path: str, temperature: float = 0.15, max_new_tokens: int = 1000) -> LocalLlamaLLM:
    """Load a local Llama model."""
    return LocalLlamaLLM(
        model_path=model_path,
        temperature=temperature,
        max_new_tokens=max_new_tokens
    )