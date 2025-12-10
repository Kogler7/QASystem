from typing import List, Tuple
import os
import re
import jieba
import logging

import edc.utils.llm_utils as llm_utils
from edc.utils.neo4j_helper import OpenKGHelper

logger = logging.getLogger(__name__)

class Extractor:
    # The class to handle the first stage: Open Information Extraction
    def __init__(self, 
                 openai_model=None,
                 error_prompt_dir: str = None,
                 min_triples: int = 0,
                 do_correction: bool = True) -> None:
        self.openai_model = openai_model
        self.rules = {"min_triples": min_triples}
        self.do_correction = do_correction
        self.error_prompt_dir = error_prompt_dir
        self.error_prompts = {}  # 缓存 prompt

        self.kg_helper = OpenKGHelper()

        self._load_error_prompts()

        
    def extract(
        self,
        input_text_str: str,
        head_entity: str,
        prompt_template_str: str
    ) -> List[List[str]]:
        few_shot_examples_str = self._get_few_shot_examples(head_entity)
        if not input_text_str.strip():
            logger.warning(f"[Skip] No relevant text found for entity: {head_entity}")
            return []
        filled_prompt = prompt_template_str.format_map(
            {
                "head_entity": head_entity,
                "few_shot_examples": few_shot_examples_str,
                "input_text": input_text_str,
            }
        )

        messages = [{"role": "user", "content": filled_prompt}]

        completion = llm_utils.openai_chat_completion(self.openai_model, None, messages)
        logging.debug(f"Extraction completion for entity '{head_entity}': {completion}")
        # extracted_triplets_list = llm_utils.parse_raw_triplets(completion)
        # return extracted_triplets_list
        if completion is None:
            return []
        triples = llm_utils.parse_raw_triplets(completion)
        if not triples:
            return []
        if self.do_correction:
            triples = self.verify_and_correct(triples, head_entity)

        return triples

    
    # TODO 考虑增加多轮检测，考虑增加多种错误检测,格式检测从llm加进来
    # TODO Format error：解析失败（解析也加进来）
    # TODO General conflict （需要wekipedia）
    def verify_and_correct(self, triples: List[List[str]], entity: str) -> List[List[str]]:
        error_type = ""
        # 检查数量
        if len(triples) < self.rules["min_triples"]:
            error_type = "QuantityTooSmall"
        
        # 检查格式 & head & tail
        valid_triples = []
        for triple in triples:
            if len(triple) != 3:
                error_type = "FormatError"
                break
            if triple[0] != entity:
                error_type = "HeadEntityError"
                break
            if triple[0] == triple[2]:
                error_type = "Contradiction"
                break


        if error_type != "":
            logger.debug(f"Triples {triples} found error: {error_type}, reprompting...")
            return self._reprompt(entity, error_type)
        return triples

    # TODO 只返回符合要求的
    # TODO 纠错时输入之前内容
    def _reprompt(self, entity: str, error_type: str) -> List[Tuple]:
        """根据错误类型重新生成三元组"""
        error_prompt = self.error_prompts.get(error_type, "Please generate it again.")
        # HeadEntityError 需要填入头实体
        if error_type == "HeadEntityError":
            error_prompt = error_prompt.format(entity=entity)
        
        message = [{"role": "user", "content": error_prompt}]

        completion = llm_utils.openai_chat_completion(self.openai_model, None, message)

        # return llm_utils.parse_raw_entities(completion)
        return llm_utils.parse_raw_triplets(completion)
    
    def _load_error_prompts(self):
        """从目录读取错误 prompt"""
        if self.error_prompt_dir is None:
            return
        for fname in os.listdir(self.error_prompt_dir):
            if fname.endswith(".txt"):
                key = fname.replace(".txt", "")
                with open(os.path.join(self.error_prompt_dir, fname), "r", encoding="utf-8") as f:
                    self.error_prompts[key] = f.read().strip()

    
    def _get_few_shot_examples(self, entity: str, max_examples: int = 10) -> str:
        """
        从 Ownthing 检索 few-shot examples
        """
        # 查询实体对应的三元组
        triples = self.kg_helper.query_by_head_entity(entity, limit=max_examples)
        
        # 找不到，拆分子实体
        if not triples:
            sub_entities = self._tokenize_entity(entity)
            for sub in sub_entities:
                triples += self.kg_helper.query_by_head_entity(sub, limit=max_examples)
                if triples:
                    break

        # fallback，随机取三元组
        if not triples:
            triples = self.kg_helper.get_random_triples(max_examples)

        # 拼接为 few-shot 示例字符串
        lines = []
        for rec in triples:
            # 兼容两种返回格式：dict 或 (h,r,t) 三元组
            if isinstance(rec, dict):
                h = rec.get("subject") or rec.get("s") or rec.get("head")
                r = rec.get("predicate") or rec.get("pred") or rec.get("relation")
                t = rec.get("object") or rec.get("o") or rec.get("tail")
            else:
                try:
                    h, r, t = rec
                except Exception:
                    # 无法解包，跳过
                    continue

            if h and r and t:
                lines.append([str(h), str(r), str(t)])
        logger.debug(f"Few-shot examples for entity '{entity}': {lines}")
        return str(lines)

    @staticmethod
    def _tokenize_entity(entity: str) -> List[str]:
        """
        实体分词
        """
        # 移除下划线并用空格替换
        cleaned_entity = entity.replace("_", " ").strip()
        
        # 使用 jieba 进行分词
        words = jieba.cut(cleaned_entity, cut_all=False)
        
        # 过滤掉空字符串和单个字符（除非是中文单字）
        tokens = []
        for word in words:
            word = word.strip()
            if word:
                # 对于中文，保留单字；对于英文，只保留长度大于1的单词
                if any('\u4e00' <= char <= '\u9fff' for char in word):
                    tokens.append(word)
                elif len(word) > 1:
                    tokens.append(word)
        
        return tokens