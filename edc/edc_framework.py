from edc.extract import Extractor
from edc.schema_definition import SchemaDefiner
from edc.schema_canonicalization import SchemaCanonicalizer
from edc.entity_extraction import EntityExtractor
from edc.domain_retriever import DomainRetriever
from edc.utils.neo4j_helper import ManufacturingKGHelper
from edc.pruner_classifier import PrunerClassifier
from typing import List, Dict, Any
from transformers import AutoModelForCausalLM, AutoTokenizer
from edc.schema_retriever import SchemaRetriever
from tqdm import tqdm
import os
import csv
import pathlib
import logging
from sentence_transformers import SentenceTransformer
from importlib import reload
from datetime import datetime
import json
import numpy as np
import torch

reload(logging)
logger = logging.getLogger(__name__)

import time
from datetime import datetime

# TODO： 部分参数任待提取
class EDC:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config

        # ------------------------ 核心配置 ------------------------
        core_cfg = config.get("core", {})
        self.max_layer: int = core_cfg.get("max_layer", 2)
        self.initial_head_entity_path: str = core_cfg["initial_head_entity_path"]
        self.initial_schema_path: str = core_cfg.get("initial_schema_path")
        self.enrich_schema: bool = core_cfg.get("enrich_schema", True)
        self.log_level = core_cfg.get("loglevel", "INFO")
        self.output_dir = core_cfg.get("output_dir", "./output/tmp")

        # 日志级别处理
        level = getattr(logging, self.log_level.upper(), logging.INFO)
        # logging.basicConfig(level=level)
        root = logging.getLogger()
        root.setLevel(level)

        for lib in ["httpcore", "httpx", "transformers", "urllib3"]:
            logging.getLogger(lib).setLevel(logging.WARNING)

        for h in root.handlers:
            h.setLevel(level)
        logger.info(f"Setting log level to {self.log_level} ({level})")

        # ------------------------ 模块配置 ------------------------
        self.extract_cfg = config.get("extract_triples", {})
        self.oie_cfg = config.get("oie", {})
        self.sd_cfg = config.get("schema_def", {})
        self.sc_cfg = config.get("schema_canon", {})
        self.refine_cfg = config.get("refinement", {})
        self.classifier_cfg = config.get("classifier", {})

        # ------------------------ 加载 schema ------------------------
        self.schema = {}
        if self.initial_schema_path and os.path.exists(self.initial_schema_path):
            with open(self.initial_schema_path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                for relation, definition in reader:
                    self.schema[relation] = definition

        self.loaded_model_dict = {}
        logger.info("Loaded EDC config.")

    
    def _load_model(self, model_name, model_type):
        assert model_type in ["sts", "hf"]  # Either a sentence transformer or a huggingface LLM
        if model_name in self.loaded_model_dict:
            logger.info(f"Model {model_name} is already loaded, reusing it.")
        else:
            logger.info(f"Loading model {model_name}")
            if model_type == "hf":
                model, tokenizer = (
                    AutoModelForCausalLM.from_pretrained(model_name, device_map="auto"),
                    AutoTokenizer.from_pretrained(model_name),
                )
                self.loaded_model_dict[model_name] = (model, tokenizer)
            elif model_type == "sts":
                model = SentenceTransformer(model_name, trust_remote_code=True)
                self.loaded_model_dict[model_name] = model
        return self.loaded_model_dict[model_name]
    
    
    # ------------------------ 创建输出目录（加时间戳） ------------------------
    def _ensure_output_dirs(self, output_dir: str, max_layer: int) -> str:
        if output_dir is None:
            raise ValueError("output_dir must be specified")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        final_output_dir = f"{output_dir}_{timestamp}"
        pathlib.Path(final_output_dir).mkdir(parents=True, exist_ok=True)

        for iteration in range(max_layer + 1):
            pathlib.Path(f"{final_output_dir}/iter{iteration}").mkdir(parents=True, exist_ok=True)

        return final_output_dir

    # ------------------------ 加载 prompt ------------------------
    def _load_prompt(self, path: str) -> str:
        if not path or not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # ------------------------ 核心方法：提取知识图谱 ------------------------
    def extract_kg(self):
        # 创建输出目录（带时间戳）
        self.output_dir = self._ensure_output_dirs(self.output_dir, self.max_layer)

        logger.info("EDC starts running...")

        # 加载初始头实体
        if not os.path.exists(self.initial_head_entity_path):
            raise FileNotFoundError(self.initial_head_entity_path)

        with open(self.initial_head_entity_path, "r", encoding="utf-8") as f:
            initial_head_entities = {line.strip() for line in f if line.strip()}

        # 加载 prompt template 和 few shot examples
        extract_prompt = self._load_prompt(self.extract_cfg.get("prompt_template"))
        sd_prompt_template = self._load_prompt(self.sd_cfg.get("prompt_template"))
        sd_few_shot_examples = self._load_prompt(self.sd_cfg.get("few_shot_examples"))
        sc_verify_prompt_template = self._load_prompt(self.sc_cfg.get("prompt_template"))

        # 初始化各个模块
        extractor = Extractor()
        domain_retriever = DomainRetriever()
        schema_definer = SchemaDefiner(openai_model=self.sd_cfg.get("llm"))
        sc_embedder = self._load_model(self.sc_cfg.get("embedder"), "sts")
        schema_canonicalizer = SchemaCanonicalizer(self.schema, sc_embedder, verify_openai_model=self.sc_cfg.get("llm"))
        pruner_classifier = PrunerClassifier(model_dir="/workspace/llmkg/edc-main/t5_pruner_best")
        neo4j_helper = ManufacturingKGHelper()

        batch_size = self.classifier_cfg.get("batch_size", 16)

        all_head_entity = set(initial_head_entities)
        current_heads = set(initial_head_entities)

        for epoch in range(1, self.max_layer + 1):
            logger.info(f"Layer[{epoch}/{self.max_layer}]...")
            next_heads_raw = []
            epoch_canonical_triples = []

            iteration_result_dir = f"{self.output_dir}/iter{epoch}"
            meta_path = os.path.join(iteration_result_dir, "meta.jsonl")
            final_result_path = os.path.join(iteration_result_dir, "canon_kg.txt")

            # 使用 with 确保文件句柄安全
            with open(meta_path, "w", encoding="utf-8") as meta_fh, \
                 open(final_result_path, "w", encoding="utf-8") as final_result_file:

                for head_entity in tqdm(current_heads):
                    logger.debug(f"Processing head entity: {head_entity}")

                    input_text = domain_retriever.get_text_by_entity(head_entity)
                    logger.debug(f"Retrieved text for entity '{head_entity}': {input_text}")
                    if not input_text or not input_text.strip():
                        logger.warning(f"[Skip] No relevant text for entity: {head_entity}")
                        continue

                    # 提取三元组
                    oie_triples = extractor.extract(
                        input_text_str=input_text,
                        head_entity=head_entity,
                        prompt_template_str=extract_prompt
                    )
                    if not oie_triples:
                        continue

                    # 定义关系
                    schema_definition_dict = schema_definer.define_schema(
                        input_text,
                        oie_triples,
                        sd_few_shot_examples,
                        sd_prompt_template
                    ) or {}

                    logger.debug(f"Schema definitions for entity '{head_entity}': {schema_definition_dict}")

                    # 规范化
                    canonicalized_triplets = []
                    canon_candidate_dict_list = []
                    for oie_triplet in oie_triples:
                        canonicalized_triplet, canon_candidate_dict = schema_canonicalizer.canonicalize(
                            input_text,
                            oie_triplet,
                            schema_definition_dict,
                            sc_verify_prompt_template,
                            self.enrich_schema
                        )
                        if canonicalized_triplet is not None:
                            canonicalized_triplets.append(canonicalized_triplet)
                        canon_candidate_dict_list.append(canon_candidate_dict)

                    # 收集尾实体（确保是字符串）
                    for triple in oie_triples:
                        if isinstance(triple, (list, tuple)) and len(triple) >= 3:
                            tail = triple[2]
                            if isinstance(tail, str) and tail.strip():
                                next_heads_raw.append(tail.strip())

                    # LOWP: 写入input-text改成写入对应索引
                    # 写 meta 文件
                    meta = {
                        "head": head_entity,
                        "input_text_snippet": input_text,
                        "oie": oie_triples,
                        "schema_definition": schema_definition_dict,
                        "canonicalization_candidates": canon_candidate_dict_list,
                        "canonicalized": canonicalized_triplets
                    }
                    # Ensure all numeric types (e.g., numpy.float32, torch tensors) are converted to
                    # native Python types so json.dumps doesn't raise TypeError: float32 not JSON serializable
                    def _sanitize(obj):
                        # primitives
                        if obj is None or isinstance(obj, (str, bool, int, float)):
                            return obj
                        # numpy scalars
                        if isinstance(obj, np.generic):
                            return obj.item()
                        # numpy arrays
                        if isinstance(obj, np.ndarray):
                            return obj.tolist()
                        # torch tensors
                        if isinstance(obj, torch.Tensor):
                            try:
                                return obj.detach().cpu().numpy().tolist()
                            except Exception:
                                return obj.tolist()
                        # dicts
                        if isinstance(obj, dict):
                            return {k: _sanitize(v) for k, v in obj.items()}
                        # lists/tuples/sets
                        if isinstance(obj, (list, tuple, set)):
                            return [_sanitize(v) for v in obj]
                        # fallback to string
                        try:
                            return str(obj)
                        except Exception:
                            return None

                    safe_meta = _sanitize(meta)
                    meta_fh.write(json.dumps(safe_meta, ensure_ascii=False) + "\n")

                    # 写规范化 KG 文件
                    for canon_trip in canonicalized_triplets:
                        final_result_file.write(json.dumps({"triple": canon_trip}, ensure_ascii=False) + "\n")

                    epoch_canonical_triples.extend(canonicalized_triplets)

            # 写入 Neo4j
            if epoch_canonical_triples:
                logger.info(f"Writing {len(epoch_canonical_triples)} triples of epoch {epoch} to Neo4j...")
                ok = neo4j_helper.batch_store_triples_from_list(epoch_canonical_triples)
                if ok:
                    logger.info(f"Epoch {epoch} triples inserted successfully.")
                else:
                    logger.error(f"Neo4j batch write failed at epoch {epoch}")

            # 去重并过滤已有 head
            unique_raw = list(dict.fromkeys(next_heads_raw))
            unique_unseen = [t for t in unique_raw if t not in all_head_entity]
            logger.info(f"Candidate tails after dedup & unseen filter: {len(unique_unseen)}")

            # 分类器过滤
            if pruner_classifier:
                preds = pruner_classifier.batch_predict(unique_unseen)
                assert len(preds) == len(unique_unseen), "Classifier returned mismatched length"
                next_heads = {tail for tail, label in zip(unique_unseen, preds) if label.lower() == "growing"}
            else:
                next_heads = set(unique_unseen)

            # 更新已见 head
            all_head_entity.update(next_heads)
            current_heads = next_heads

            if not current_heads:
                logger.info("No more head entities after pruning. Stopping early.")
                break

        logger.info("EDC finished running.")
        return

        
    


