import os
import json
# import openai
from openai import OpenAI
import time
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig
import ast
from sentence_transformers import SentenceTransformer
from typing import List
import gc
import torch
import logging
import requests

logger = logging.getLogger(__name__)

API_URL = "http://10.126.62.88:8000/v1/completions"
API_KEY = ""

def free_model(model: AutoModelForCausalLM = None, tokenizer: AutoTokenizer = None):
    try:
        model.cpu()
        if model is not None:
            del model
        if tokenizer is not None:
            del tokenizer
        gc.collect()
        torch.cuda.empty_cache()
    except Exception as e:
        logger.warning(e)


def get_embedding_e5mistral(model, tokenizer, sentence, task=None):
    model.eval()
    device = model.device

    if task != None:
        # It's a query to be embed
        sentence = get_detailed_instruct(task, sentence)

    sentence = [sentence]

    max_length = 4096
    # Tokenize the input texts
    batch_dict = tokenizer(
        sentence, max_length=max_length - 1, return_attention_mask=False, padding=False, truncation=True
    )
    # append eos_token_id to every input_ids
    batch_dict["input_ids"] = [input_ids + [tokenizer.eos_token_id] for input_ids in batch_dict["input_ids"]]
    batch_dict = tokenizer.pad(batch_dict, padding=True, return_attention_mask=True, return_tensors="pt")

    batch_dict.to(device)

    embeddings = model(**batch_dict).detach().cpu()

    assert len(embeddings) == 1

    return embeddings[0]


def get_detailed_instruct(task_description: str, query: str) -> str:
    return f"Instruct: {task_description}\nQuery: {query}"


def get_embedding_sts(model: SentenceTransformer, text: str, prompt_name=None, prompt=None):
    embedding = model.encode(text, prompt_name=prompt_name, prompt=prompt)
    return embedding


def parse_raw_entities(raw_entities: str):
    parsed_entities = []
    left_bracket_idx = raw_entities.index("[")
    right_bracket_idx = raw_entities.index("]")
    try:
        parsed_entities = ast.literal_eval(raw_entities[left_bracket_idx : right_bracket_idx + 1])
    except Exception as e:
        pass
    logging.debug(f"Entities {raw_entities} parsed as {parsed_entities}")
    return parsed_entities


# def parse_raw_triplets(raw_triplets: str):
#     # Look for enclosing brackets
#     unmatched_left_bracket_indices = []
#     matched_bracket_pairs = []

#     collected_triples = []
#     for c_idx, c in enumerate(raw_triplets):
#         if c == "[":
#             unmatched_left_bracket_indices.append(c_idx)
#         if c == "]":
#             if len(unmatched_left_bracket_indices) == 0:
#                 continue
#             # Found a right bracket, match to the last found left bracket
#             matched_left_bracket_idx = unmatched_left_bracket_indices.pop()
#             matched_bracket_pairs.append((matched_left_bracket_idx, c_idx))
#     for l, r in matched_bracket_pairs:
#         bracketed_str = raw_triplets[l : r + 1]
#         try:
#             parsed_triple = ast.literal_eval(bracketed_str)
#             if len(parsed_triple) == 3 and all([isinstance(t, str) for t in parsed_triple]):
#                 if all([e != "" and e != "_" for e in parsed_triple]):
#                     collected_triples.append(parsed_triple)
#             elif not all([type(x) == type(parsed_triple[0]) for x in parsed_triple]):
#                 for e_idx, e in enumerate(parsed_triple):
#                     if isinstance(e, list):
#                         parsed_triple[e_idx] = ", ".join(e)
#                 collected_triples.append(parsed_triple)
#         except Exception as e:
#             pass
#     logger.debug(f"Triplets {raw_triplets} parsed as {collected_triples}")
#     return collected_triples

# TODO 更改三元组解析函数
def parse_raw_triplets(raw_triplets: str):
    import re

    if "[" not in raw_triplets or "]" not in raw_triplets:
        return []

    # remove common unicode minus and fix
    raw_triplets = raw_triplets.replace("−", "-")

    # strip leading model control tokens like <|assistant|>, <|start_of_response|>, etc.
    raw_triplets = re.sub(r"^\s*<\|[^>]+\|>\s*", "", raw_triplets)
    # remove any other inline control tokens
    raw_triplets = re.sub(r"<\|[^>]+\|>", "", raw_triplets)
    # remove non-printable/control characters
    raw_triplets = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", raw_triplets)

    # Try to find an outermost nested list like [[...]] using regex (DOTALL)
    m = re.search(r"\[\s*\[.*?\]\s*\]", raw_triplets, re.DOTALL)
    candidates = []
    if m:
        candidates.append(m.group(0))

    # Fallback: collect all bracket pairs (original algorithm)
    unmatched_left_bracket_indices = []
    matched_bracket_pairs = []
    for c_idx, c in enumerate(raw_triplets):
        if c == "[":
            unmatched_left_bracket_indices.append(c_idx)
        elif c == "]" and unmatched_left_bracket_indices:
            l = unmatched_left_bracket_indices.pop()
            matched_bracket_pairs.append((l, c_idx))

    for l, r in matched_bracket_pairs:
        candidates.append(raw_triplets[l : r + 1])

    collected_triples = []
    for bracketed_str in candidates:
        try:
            parsed = ast.literal_eval(bracketed_str)
        except Exception as e:
            logger.debug(f"Failed ast.literal_eval on candidate: {bracketed_str[:200]}..., error: {e}")
            continue

        # If parsed is a list of triples (list of lists), normalize each triple
        if isinstance(parsed, list) and parsed and all(isinstance(x, list) and len(x) == 3 for x in parsed):
            for parsed_triple in parsed:
                parsed_triple = [str(e) if not isinstance(e, str) else e for e in parsed_triple]
                if all(e not in ["", "_"] for e in parsed_triple):
                    # flatten nested lists inside triple elements
                    for i, e in enumerate(parsed_triple):
                        if isinstance(e, list):
                            parsed_triple[i] = ", ".join(map(str, e))
                    collected_triples.append(parsed_triple)
        # Or if parsed itself is a single triple
        elif isinstance(parsed, list) and len(parsed) == 3 and all(not isinstance(x, list) for x in parsed):
            parsed_triple = [str(e) if not isinstance(e, str) else e for e in parsed]
            if all(e not in ["", "_"] for e in parsed_triple):
                collected_triples.append(parsed_triple)

    logger.debug(f"Triplets parsed from raw: {collected_triples}")
    return collected_triples

def parse_relation_definition(raw_definitions: str):
    descriptions = raw_definitions.split("\n")
    relation_definition_dict = {}

    for description in descriptions:
        if ":" not in description:
            continue
        index_of_colon = description.index(":")
        relation = description[:index_of_colon].strip()

        relation_description = description[index_of_colon + 1 :].strip()

        if relation == "Answer":
            continue

        relation_definition_dict[relation] = relation_description
    logger.debug(f"Relation Definitions {raw_definitions} parsed as {relation_definition_dict}")
    return relation_definition_dict


def is_model_openai(model_name):
    return "gpt" in model_name


def generate_completion_transformers(
    input: list,
    model: AutoModelForCausalLM,
    tokenizer: AutoTokenizer,
    max_new_token=256,
    answer_prepend="",
):
    device = model.device
    tokenizer.pad_token = tokenizer.eos_token

    messages = tokenizer.apply_chat_template(input, add_generation_prompt=True, tokenize=False) + answer_prepend

    model_inputs = tokenizer(messages, return_tensors="pt", padding=True, add_special_tokens=False).to(device)

    generation_config = GenerationConfig(
        do_sample=False,
        max_new_tokens=max_new_token,
        pad_token_id=tokenizer.eos_token_id,
        return_dict_in_generate=True,
    )

    generation = model.generate(**model_inputs, generation_config=generation_config)
    sequences = generation["sequences"]
    generated_ids = sequences[:, model_inputs["input_ids"].shape[1] :]
    generated_texts = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()

    logging.debug(f"Prompt:\n {messages}\n Result: {generated_texts}")
    return generated_texts

def openai_chat_completion(
    model,
    system_prompt,
    history,
    temperature=0,
    max_tokens=512,
    max_retries=3,
    retry_sleep=2,
):

    client = OpenAI(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    messages = (
        [{"role": "system", "content": system_prompt}] + history
        if system_prompt
        else history
    )

    for attempt in range(1, max_retries + 1):
        try:
            # response = client.chat.completions.create(
            #     model="qwen3-32b",
            #     messages=messages,
            #     temperature=temperature,
            #     max_tokens=max_tokens,
            #     extra_body={"enable_thinking": False},
            # )

            response = client.chat.completions.create(
                model="qwen3-30b-a3b",
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body={"enable_thinking": False},
            )

            content = response.choices[0].message.content

            # logging.debug(
            #     f"[SUCCESS] attempt={attempt} model={model}\n"
            #     f"Prompt:\n{messages}\n"
            #     f"Result: {content}"
            # )
            return content

        except Exception as e:
            logging.warning(
                f"[FAILED] attempt={attempt}/{max_retries} error: {e}"
            )
            if attempt == max_retries:
                raise
            time.sleep(retry_sleep)

    raise RuntimeError("Unexpected error in ChatCompletion retry logic")


# def openai_chat_completion(model, system_prompt, history, temperature=0, max_tokens=512):
#     openai.api_key = os.environ["OPENAI_KEY"]
#     response = None
#     if system_prompt is not None:
#         messages = [{"role": "system", "content": system_prompt}] + history
#     else:
#         messages = history
#     while response is None:
#         try:
#             response = openai.chat.completions.create(
#                 model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
#             )
#         except Exception as e:
#             time.sleep(5)
#     logging.debug(f"Model: {model}\nPrompt:\n {messages}\n Result: {response.choices[0].message.content}")
#     return response.choices[0].message.content

# # TODO 模型直接设置为配置文件
# def openai_chat_completion(
#     model,
#     system_prompt,
#     history,
#     temperature=0,
#     max_tokens=None
# ):
#     if system_prompt is not None:
#         messages = [{"role": "system", "content": system_prompt}] + history
#     else:
#         messages = history

#     # 构造 ChatML
#     prompt = ""
#     for msg in messages:
#         role = msg["role"]
#         content = msg["content"]
#         prompt += f"<|{role}|>\n{content}\n"
#     prompt += "<|assistant|>\n"

#     payload = {
#         "model": "Qwen/Qwen3-8B",
#         "prompt": prompt,
#         "temperature": temperature,
#     }

#     if max_tokens is not None:
#         payload["max_tokens"] = max_tokens 

#     headers = {
#         "Content-Type": "application/json",
#         "Authorization": f"Bearer {API_KEY}"
#     }

#     response_json = None
#     for retry in range(3):
#         try:
#             resp = requests.post(API_URL, headers=headers, data=json.dumps(payload))
#             if resp.status_code == 200:
#                 response_json = resp.json()
#                 break
#             else:
#                 logging.error(f"vLLM HTTP {resp.status_code}: {resp.text}")
#         except Exception as e:
#             logging.error(f"vLLM 请求失败: {e}")
#         time.sleep(3)

#     if response_json is None:
#         raise RuntimeError("vLLM: 多次重试仍失败")

#     text = response_json["choices"][0]["text"]
#     logging.debug(f"Prompt:\n{prompt}\nResult: {text}")
