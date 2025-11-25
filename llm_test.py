import requests
import json

API_URL = "http://127.0.0.1:8000/v1/completions"
API_KEY = ""

def test_vllm():
    payload = {
        "model": "Qwen/Qwen3-8B",
        "prompt": "你好，请用一句话介绍你自己。",
        "max_tokens": 50
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }

    try:
        response = requests.post(API_URL, headers=headers, data=json.dumps(payload))
        if response.status_code == 200:
            result = response.json()
            print("vLLM 测试成功！\n")
            print("模型输出：", result['choices'][0]['text'])
        else:
            print(f"请求失败，状态码: {response.status_code}")
            print(response.text)
    except Exception as e:
        print("请求出错：", e)

if __name__ == "__main__":
    test_vllm()
