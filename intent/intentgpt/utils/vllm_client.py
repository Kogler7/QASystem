"""vLLM API Client for LangChain integration.

This module provides a LangChain-compatible interface for calling vLLM API endpoints,
replacing the local Llama model loading approach with a more efficient API-based solution.
"""

import os
import requests
import json
from typing import Optional, List, Any, Dict
from langchain_core.language_models.llms import LLM
from pydantic import Field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type
)


class VLLMAPIError(Exception):
    """Custom exception for vLLM API errors."""
    pass


class VLLMClient(LLM):
    """
    vLLM API Client compatible with LangChain LLM interface.
    
    This client connects to a vLLM server endpoint and provides the same
    interface as local LLM models, making it a drop-in replacement for
    local Llama model loading.
    
    Attributes:
        api_url: The vLLM API endpoint URL
        model: The model name to use (e.g., "Qwen/Qwen3-8B")
        temperature: Sampling temperature (0.0 to 1.0)
        max_new_tokens: Maximum tokens to generate
        api_key: Optional API key for authentication
        timeout: Request timeout in seconds
        top_p: Nucleus sampling parameter
    
    Example:
        >>> client = VLLMClient(
        ...     api_url="http://127.0.0.1:8000/v1/completions",
        ...     model="Qwen/Qwen3-8B",
        ...     temperature=0.15
        ... )
        >>> response = client("你好,请介绍一下你自己")
    """
    
    api_url: str = Field(default="http://127.0.0.1:8000/v1/completions")
    model: str = Field(default="Qwen/Qwen3-8B")
    temperature: float = Field(default=0.15)
    max_new_tokens: int = Field(default=1000)
    api_key: str = Field(default="")
    timeout: int = Field(default=120)
    top_p: float = Field(default=0.9)
    
    class Config:
        arbitrary_types_allowed = True
    
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:8000/v1/completions",
        model: str = "Qwen/Qwen3-8B",
        temperature: float = 0.15,
        max_new_tokens: int = 1000,
        api_key: str = "",
        timeout: int = 120,
        **kwargs
    ):
        """
        Initialize the vLLM API client.
        
        Args:
            api_url: vLLM API endpoint
            model: Model name to use
            temperature: Sampling temperature
            max_new_tokens: Maximum tokens to generate
            api_key: Optional API key
            timeout: Request timeout in seconds
            **kwargs: Additional arguments for LLM base class
        """
        super().__init__(
            api_url=api_url,
            model=model,
            temperature=temperature,
            max_new_tokens=max_new_tokens,
            api_key=api_key,
            timeout=timeout,
            **kwargs
        )
        self._validate_connection()
    
    def _validate_connection(self):
        """Validate that the vLLM API is accessible."""
        try:
            # Try a simple health check or minimal request
            print(f"Connecting to vLLM API: {self.api_url}")
            print(f"Using model: {self.model}")
            # Note: We don't test the connection here to avoid unnecessary API calls
            # The actual connection will be validated on first use
        except Exception as e:
            print(f"Warning: Could not validate vLLM API connection: {e}")
    
    @property
    def _llm_type(self) -> str:
        """Return type of LLM."""
        return "vllm_api"
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(VLLMAPIError)
    )
    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        **kwargs
    ) -> str:
        """
        Call the vLLM API with the given prompt.
        
        Args:
            prompt: The input prompt
            stop: Optional stop sequences
            **kwargs: Additional generation parameters
            
        Returns:
            Generated text response
            
        Raises:
            VLLMAPIError: If API call fails
        """
        # Prepare the request payload
        payload = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": kwargs.get("max_tokens", self.max_new_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
        }
        
        # Add stop sequences if provided
        if stop:
            payload["stop"] = stop
        
        # Prepare headers
        headers = {
            "Content-Type": "application/json"
        }
        
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        try:
            # Make the API request
            response = requests.post(
                self.api_url,
                headers=headers,
                data=json.dumps(payload),
                timeout=self.timeout
            )
            
            # Check for HTTP errors
            if response.status_code != 200:
                error_msg = f"vLLM API error (status {response.status_code}): {response.text}"
                print(f"[ERROR] {error_msg}")
                raise VLLMAPIError(error_msg)
            
            # Parse response
            result = response.json()
            
            # Extract generated text
            if "choices" in result and len(result["choices"]) > 0:
                generated_text = result["choices"][0].get("text", "")
                return generated_text.strip()
            else:
                error_msg = f"Unexpected API response format: {result}"
                print(f"[ERROR] {error_msg}")
                raise VLLMAPIError(error_msg)
                
        except requests.exceptions.Timeout:
            error_msg = f"Request timeout after {self.timeout} seconds"
            print(f"[ERROR] {error_msg}")
            raise VLLMAPIError(error_msg)
        
        except requests.exceptions.ConnectionError:
            error_msg = f"Failed to connect to vLLM API at {self.api_url}"
            print(f"[ERROR] {error_msg}")
            print("[提示] Ensure vLLM server is running:")
            print(f"   python -m vllm.entrypoints.api_server --model {self.model}")
            raise VLLMAPIError(error_msg)
        
        except json.JSONDecodeError as e:
            error_msg = f"Failed to parse API response: {e}"
            print(f"[ERROR] {error_msg}")
            raise VLLMAPIError(error_msg)
        
        except Exception as e:
            error_msg = f"Unexpected error calling vLLM API: {str(e)}"
            print(f"[ERROR] {error_msg}")
            raise VLLMAPIError(error_msg)


def load_vllm_client(
    api_url: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.15,
    max_new_tokens: int = 1000,
    api_key: Optional[str] = None
) -> VLLMClient:
    """
    Factory function to create a VLLMClient instance.
    
    This function provides a convenient way to create a client with
    sensible defaults from environment variables.
    
    Args:
        api_url: vLLM API endpoint (defaults to env var VLLM_API_URL)
        model: Model name (defaults to env var VLLM_MODEL)
        temperature: Sampling temperature
        max_new_tokens: Maximum tokens to generate
        api_key: API key (defaults to env var VLLM_API_KEY)
        
    Returns:
        Configured VLLMClient instance
        
    Example:
        >>> # Using environment variables
        >>> client = load_vllm_client()
        >>> 
        >>> # Or with explicit parameters
        >>> client = load_vllm_client(
        ...     api_url="http://localhost:8000/v1/completions",
        ...     model="Qwen/Qwen3-8B",
        ...     temperature=0.12
        ... )
    """
    # Get defaults from environment variables
    api_url = api_url or os.getenv("VLLM_API_URL", "http://127.0.0.1:8000/v1/completions")
    model = model or os.getenv("VLLM_MODEL", "Qwen/Qwen3-8B")
    api_key = api_key or os.getenv("VLLM_API_KEY", "")
    
    return VLLMClient(
        api_url=api_url,
        model=model,
        temperature=temperature,
        max_new_tokens=max_new_tokens,
        api_key=api_key
    )


# Alias for backward compatibility with local_llm module
load_local_llama = load_vllm_client


if __name__ == "__main__":
    # Simple test when run directly
    print("Testing vLLM API Client...")
    print("-" * 60)
    
    client = load_vllm_client()
    
    test_prompt = "你好,请用一句话介绍你自己。"
    print(f"Prompt: {test_prompt}")
    print("-" * 60)
    
    try:
        response = client(test_prompt)
        print(f"[OK] Response: {response}")
    except Exception as e:
        print(f"[ERROR] Test failed: {e}")