from openai import OpenAI
from pathlib import Path
from transformers import AutoTokenizer,AutoModelForCausalLM
import torch

def call_model_by_api(query,temperature=0,max_tokens=50):
    client = OpenAI(
        base_url="http://10.126.62.88:8001/v1",
        api_key="none"
    )

    try:
        response = client.completions.create(
            prompt=query,
            temperature=temperature,
            max_tokens=max_tokens,
            model="qwen-intent-model"
        )
        return response.choices[0].text

    except Exception as e:
        return f"调用失败，原因：{str(e)}"




