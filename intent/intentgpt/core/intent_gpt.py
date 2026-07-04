import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set parallelism
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'true')

from langchain_core.prompts import PromptTemplate
from langchain_classic.chains import LLMChain
from sklearn.cluster import DBSCAN


from ..data.dataloaders import MyData
from sklearn.cluster import KMeans
import pickle

import time

from ..utils.metrics import clustering_score
from ..utils.experiment_utils import hash_dict
from ..utils.prompt_utils import parse_output


from sklearn.preprocessing import LabelEncoder

import pandas as pd
from datetime import datetime

from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)
from omegaconf import OmegaConf
from tqdm import tqdm
import re
import wandb
import random

from .embeddings import Embedder, EmbeddingDB
from ..utils.vllm_client import load_vllm_client

    
class IntentGPT():

    def __init__(self, args):        
        self.args = args
        self.data = self.get_data()
        
        # Note: Attack mode is disabled in local Llama version for simplicity
        # You can re-enable it by creating a separate local LLM instance if needed
        self.attack = False
        if args.attack:
            print("Warning: Attack mode is disabled in local Llama version")
        self.scramble_test_samples = args.scramble_test_samples
        self.scramble_known_intents = args.scramble_known_intents

        self.class_map = {v: k for k, v in self.data.all_label_map.items()}
        
        self.init_llm()
        self.create_directories()

        if self.args.use_wandb:
            wandb.init(name=f"{self.readable_id}_{self.exp_id}", project=self.args.project, config=dict(self.args), entity=self.args.entity)

        if self.use_embeddings():
            self.embedder = Embedder(self.args.intent_embeddings)

        if self.args.semantic_feedback_discovery or self.args.semantic_few_shot:
            self.test_intents_embeddings = EmbeddingDB(self.embedder, db_file="test_intents_embeddings.sqlite")

        if self.args.feedback_discovered_categories:
            self.args.discovered_categories_path = f"{self.results_path}/discovered_categories.txt"
            if self.args.start_with_known_categories:
                self.known_intents = [str(it) for it in list(self.data.train_label_map.keys())]
                with open(args.discovered_categories_path, 'w') as f:
                    for item in self.known_intents:
                        f.write(f"{item}\n")
                if self.args.semantic_feedback_discovery:
                    # Here we create the Embedding database
                    self.known_intents_embeddings = EmbeddingDB(self.embedder, self.known_intents, db_file="known_intents_embeddings.sqlite")
            else:
                open(args.discovered_categories_path, 'w').close() # Create the file (empty db)            
        
        if self.args.feedback_initial_categories:
            self.known_intents = [str(it) for it in list(self.data.train_label_map.keys())]

        self.init_few_shot_pool()

        if not self.args.sample_few_shot_every_iteration:
            self.current_few_shot_samples = self.get_few_shot_examples()

    def use_embeddings(self):
        return self.args.intent_embeddings or self.args.use_embeddings_for_labels or self.args.semantic_few_shot or self.args.semantic_feedback_discovery
    
    def get_hard_prompt(self):
        dataset_name = self.args.dataset
        compute_hard_samples = self.args.compute_hard_samples
        N_hard_samples = self.args.N_hard_samples
        samples_x_class = self.args.samples_x_class
        force_recompute = self.args.force_recompute
        labeled_ratio = self.args.labeled_ratio
        known_cls_ratio = self.args.known_cls_ratio

        if compute_hard_samples:
            data_config_id = f"{labeled_ratio}_labeled_{known_cls_ratio}_known_intents_{N_hard_samples}_hard_samples_{samples_x_class}_samples_x_class"
        else:
            data_config_id = f"{labeled_ratio}_labeled_{known_cls_ratio}_known_intents_hard_prompt_{samples_x_class}_samples_x_class"
            hard_samples = None
            
        path_store_prompts = f"prompts/{dataset_name}/{data_config_id}"
        os.makedirs(path_store_prompts, exist_ok=True)

        if compute_hard_samples:
            hard_examples_path = os.path.join(path_store_prompts, 'hard_examples.txt')
        prompt_path = os.path.join(path_store_prompts, 'prompt.txt')

        # Check if prompt exists
        if os.path.exists(prompt_path) and not force_recompute:
            # extract hard_samples line by line and re find utterance and intent
            if compute_hard_samples:
                with open(hard_examples_path, 'r') as f:
                    content = f.read()
                if not content.endswith('\n'):
                    content += '\n'
                hard_samples = re.findall(r'Utterance: (.*?), Intent: (.*?)\n', content)

            with open(prompt_path, 'r') as f:
                prompt = f.read()
            
            return hard_samples, prompt
        else:
            
            print("Generating prompt...")

            # Dictionary of {class: [utterances]}
            classes = {}
            for sample in self.data.train_labeled_loader.dataset:
                # add sample to classes using label as key
                label = int(sample['labels'])
                if label not in classes:
                    classes[label] = []
                utterance = self.data.tokenizer.decode(sample['input_ids'], skip_special_tokens = True)
                classes[label].append(utterance)
            
            # order classes by key
            classes = dict(sorted(classes.items()))

            # Define class id to class name mapping
            class_map = {v: k for k, v in self.data.train_label_map.items()}

            # Filter classes in class_map that are in classes
            class_map = {k: v for k, v in class_map.items() if k in classes}
            
            # select num_samples_per_class samples per class
            dict_class_examples = {}
            for class_id, class_name in class_map.items():
                dict_class_examples[class_name] = classes[class_id][:samples_x_class]

            if compute_hard_samples:
                prompt_template = '''You are a helpful assistant and an expert in natural language processing and specialize in open set discovery intent classification. This task involves assigning textual utterances to specific intents, some of which are pre-defined (known) and others are not (unknown). \nAs an expert in open set discovery intent classification, your task is to provide a list of {N_hard_samples} pairs of utterances and intents that are the most difficult to classify. These pairs should consist of one utterance and its corresponding intent. Make sure that the final list contains high variety of intents. The hard samples you propose need to be extracted from the original list.
                
                To select the most difficult pairs, consider the following criteria:
                1. Ambiguity: Choose utterances that can be interpreted in multiple ways, making it challenging to determine the correct intent.
                2. Contextual complexity: Select pairs where the intent is highly dependent on the context provided in the utterance or surrounding dialogue.
                3. Lack of explicit keywords: Include pairs with utterances that do not contain obvious keywords or explicit indicators of the intent.
                4. Similarities among intents: Include pairs where intents have overlapping or similar meanings, making it challenging to differentiate between them.

                Once you have selected the difficult pairs, and have acquired sufficient context about the problem, provide a detailed prompt for an AI language model to solve the task of open set intent discovery, maximizing the model's performance in the task. Provide effective guidelines about how to solve the task in the prompt. 

                EXAMPLES:
                
                {train_examples}

                You must respond using the following format:    
                HARD EXAMPLES ({N_hard_samples} pairs): Utterance: <utterance>, Intent: <intent>      
                PROMPT: <prompt>
                '''
            else:
                # read file
                prompt_design_prompt = 'prompts/common_prompts/initial_prompt_gpt4.txt'
                with open(prompt_design_prompt, 'r') as f:
                    prompt_template = f.read()
                # prompt_template = '''You are a helpful assistant and an expert in natural language processing. You specialize in intent discovery. This task involves assigning textual utterances to specific intents, some of which are pre-defined and others are not. \nYou will be presented with a set of examples from the dataset, and need to acquire sufficient context and knowledge about the problem and specific data domain. As an expert, provide a detailed prompt for an AI language model to solve the task of open set intent discovery, maximizing the model's performance in the task. Provide effective guidelines about how to solve the task in the prompt.\nEXAMPLES:{train_examples}.\nYou must respond using the following format:\nPROMPT: <prompt>'''
            
            # Each example is a string of the form "Utterance: <utterance>, Intent: <intent>"
            train_examples = []
            for class_name, utterances in dict_class_examples.items():
                for utterance in utterances:
                    train_examples.append(f'Utterance: {utterance}, Intent: {class_name}')

            train_examples = '\n'.join(train_examples)

            if compute_hard_samples:
                long_prompt = PromptTemplate(template=prompt_template, input_variables=["train_examples", "N_hard_samples"])
            else:
                long_prompt = PromptTemplate(template=prompt_template, input_variables=["train_examples"])

            # Use vLLM API for prompt generation
            api_url = os.getenv('VLLM_API_URL', 'http://127.0.0.1:8000/v1/completions')
            model_name = os.getenv('VLLM_MODEL', 'Qwen/Qwen3-8B')
            api_key = os.getenv('VLLM_API_KEY', '')
            
            local_llm = load_vllm_client(
                api_url=api_url,
                model=model_name,
                temperature=0.2,
                max_new_tokens=2000,
                api_key=api_key
            )
            llm_chain = LLMChain(prompt=long_prompt, llm=local_llm)
            if self.args.verbose:
                if compute_hard_samples:
                    print(long_prompt.format(train_examples=train_examples, N_hard_samples=N_hard_samples))
                else:
                    print(long_prompt.format(train_examples=train_examples))

            @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(2))
            def completion_with_backoff(**kwargs):
                return llm_chain.run(**kwargs)
            
            if compute_hard_samples:
                output = completion_with_backoff(train_examples=train_examples, N_hard_samples=N_hard_samples)

                # Extracting hard samples
                hard_samples = re.findall(r'Utterance: (.*?), Intent: (.*?)\n', output)

                # Save results in txt
                with open(hard_examples_path, 'w') as f:
                    f.write('\n'.join([f'Utterance: {utterance}, Intent: {intent}' for utterance, intent in hard_samples]))     
            else:
                output = completion_with_backoff(train_examples=train_examples)

            # Extracting argumentation
            prompt_start = output.find('PROMPT:') + len('PROMPT:')
            prompt = output[prompt_start:].strip()
              
            with open(prompt_path, 'w') as f:
                f.write(prompt)

            return hard_samples, prompt

    def init_few_shot_pool(self):
        self.class_map_train = {v: k for k, v in self.data.train_label_map.items()}

        few_shot_pool_samples = None

        if self.args.prompt_type == 'hard_prompt':
            hard_samples, self.hard_prompt = self.get_hard_prompt()
            few_shot_pool_samples = hard_samples

        if few_shot_pool_samples == None:
            self.few_shot_pool = []
            for i in range(self.data.train_labeled_loader.dataset.__len__()):
                sample = self.data.train_labeled_loader.dataset[i]
                utterance = self.data.tokenizer.decode(sample['input_ids'], skip_special_tokens = True)
                label = self.class_map_train[int(sample['labels'])]
                self.few_shot_pool.append((utterance, label))
        else:
            self.few_shot_pool = hard_samples

        if self.args.semantic_few_shot:
            # create and embedding database for few-shot samples
            few_shot_pool = []
            for sample in self.few_shot_pool:
                utterance = str(sample[0].replace('"', ''))
                few_shot_pool.append(utterance)
            self.few_shot_embeddings = EmbeddingDB(self.embedder, few_shot_pool, db_file="few_shot_embeddings.sqlite")

    def create_directories(self):

        # --------------- EXPERIMENT INFO --------------------
        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.exp_id = hash_dict(dict(self.args)) +"_"+str(self.timestamp)

        # Create a directory for storing results
        results_path = f"results_v3/{self.args.dataset}"
        feedpack_string = 'feedback' if self.args.feedback_discovered_categories else 'no_feedback'
        self.readable_id = f"{self.args.prompt_type}_{self.args.num_few_shot_examples}_shot_{self.args.known_cls_ratio}_clsratio_{feedpack_string}_temp_{self.args.temperature}_{self.args.gpt_model}"
        self.results_path = f"{results_path}/{self.readable_id}_{self.exp_id}"
        os.makedirs(self.results_path, exist_ok=True)

        if self.args.verbose:
            print(f"Results will be saved in {results_path}")
        
        # Store config file
        with open(os.path.join(self.results_path, 'config.yaml'), 'w') as f:
            OmegaConf.save(self.args, f)

    @retry(wait=wait_random_exponential(min=2, max=200), stop=stop_after_attempt(100))
    def completion_with_backoff(self, **kwargs):
        return self.llm_chain.run(**kwargs)
    
    def get_prompt_template(self):
        return """{prompt}\nCONTEXT EXAMPLES:\n{few_shot_examples}\nRESPONSE FORMAT:\nID: <i>, Utterance: <content>, Intent: <intent> Use the same ID in the test example.\nTEST EXAMPLES (to predict):\n{test_examples}\nYour response:"""

    def init_llm(self):
        # -------------- vLLM API CLIENT ---------------
        # Load prompt template
        prompt_template = self.get_prompt_template()
        self.long_prompt = PromptTemplate(template=prompt_template, input_variables=["few_shot_examples", "test_examples", "prompt"])
        
        # Get vLLM API settings from environment variables or config
        api_url = os.getenv('VLLM_API_URL', 'http://127.0.0.1:8000/v1/completions')
        model_name = os.getenv('VLLM_MODEL', self.args.gpt_model if hasattr(self.args, 'gpt_model') else 'Qwen/Qwen3-8B')
        api_key = os.getenv('VLLM_API_KEY', '')
        
        print(f"Initializing vLLM API client:")
        print(f"  API URL: {api_url}")
        print(f"  Model: {model_name}")
        print(f"  Temperature: {self.args.temperature}")
        
        self.local_llm = load_vllm_client(
            api_url=api_url,
            model=model_name,
            temperature=self.args.temperature,
            max_new_tokens=800,  # Optimized: reduced from 1000 to improve efficiency
            api_key=api_key
        )
        self.llm_chain = LLMChain(prompt=self.long_prompt, llm=self.local_llm)

    def get_test_samples(self, i, batch, b_size, return_formatted=True):
        test_examples = []        
        gt_labels = []          
        ids = [] 
        for j in range(b_size):
            id = i * self.args.batch_size + j
            ids.append(id)
            utterance = self.data.tokenizer.decode(batch['input_ids'][j], skip_special_tokens = True)
            label = self.class_map[int(batch['labels'][j])]
            test_examples.append((id, utterance))

            if self.args.verbose:
                print(f"test example {id}: {utterance}")
            gt_labels.append(label)

        if return_formatted:
            test_examples = '\n'.join([f"ID:{id}, {utterance}" for id, utterance in test_examples])

        return test_examples, gt_labels, ids        

    def get_few_shot_examples(self):
        if self.args.k_nearest_few_shot == 0:
            return []
        if self.args.semantic_few_shot:
            test_samples_embeddings = []
            for sample in self.current_test_samples:
                utterance = sample[1]
                embedding = self.test_intents_embeddings.get_embedding(utterance)
                test_samples_embeddings.append(embedding)

            distances, indices = self.few_shot_embeddings.get_k_nearest(test_samples_embeddings, self.args.k_nearest_few_shot)
    
            few_shot_examples = []
            for i in range(len(indices)):
                for j, ind in enumerate(indices[i]):
                    few_shot_examples.append((self.few_shot_pool[ind][0], self.few_shot_pool[ind][1], distances[i][j]))

            # Keep only unique sentences, updating the distance if the current one is smaller    
            unique_examples = {}
            for sentence, intent, distance in few_shot_examples:
                if sentence not in unique_examples:
                    unique_examples[sentence] = (intent, distance)
                else:
                    # Update if current distance is smaller
                    if distance < unique_examples[sentence][1]:
                        unique_examples[sentence] = (intent, distance)
            
            # Sort by distance and keep the top k examples
            few_shot_examples = sorted(unique_examples.items(), key=lambda item: item[1][1])[:self.args.num_few_shot_examples]
            few_shot_examples = [(sentence, intent) for sentence, (intent, distance) in few_shot_examples]

        else:
            few_shot_examples = []
            random.shuffle(self.few_shot_pool)
            for i in range(self.args.num_few_shot_examples):
                few_shot_examples.append(self.few_shot_pool[i])

        ret = '\n'.join([f"Utterance: {utterance.strip()}, Intent: {intent}" for index, (utterance, intent) in enumerate(few_shot_examples)])
        return ret
    
    def get_prompt(self):
        prompt_type = self.args.prompt_type
        # This task involves assigning textual utterances to specific intents, some of which are pre-defined and others are not. You have to assign utterances to known intents, or create new intents if none of the known intents fit with the utterance. 

        base_prompt = """You are a helpful assistant and an expert in natural language processing and specialize in the task of intent detection."""
        additional_description = """Make sure each intent is only between one and three words, and as short and reusable as possible. Use the same format as the context examples. Don't classify the examples below CONTEXT EXAMPLES. Only classify the test examples below TEST EXAMPLES. You are prohibited to assign intents to 'unknown'. Instead, create a new intent."""
        if prompt_type == "base_prompt":
            prompt = f"""{base_prompt} The intent can be one of the pre-defined intents or a new one that you create based on the context and knowledge about the problem and specific data domain. Be aware of the known intents and reuse them as much as possible, but don't hesitate to create new intents when necessary. {additional_description}""" 
            
        elif prompt_type == "simple_guidelines":
            prompt = f"""
                {base_prompt}
                You must follow the following guidelines when predicting the intent:
                1) Identify Core Request: Look for verbs signaling the user's intent, such as "update", "change", "want to know", "maintainance" etc.
                2) Spot Key Subjects: Identify important objects or subjects like "pin number", "song", "time", "schedule", etc.
                3) Contextual Clues: Observe the surrounding details or scenario which can hint at the intent.
                4) Geographical/Time Terms: Note references to specific locations or time.
                5) Question vs. Declaration: Determine if the utterance is asking a question or stating a need/desire.
                6) Try to make the intent as short as possible, but make sure it is still descriptive.
                7) You can generate your prediction using some words from the utterance.
                8) Make sure the intent you choose is the most accurate intent for the utterance.
                9) Try to not generate intents that are too similar to each other.
                10) REUSE DISCOVERED INTENTS AS MUCH AS POSSIBLE, but you can be fine-grained if needed. 
                11) Try to be generate intents in an extractive manner, using words from the utterance.\n
                {additional_description}
            """
            
        elif prompt_type == "hard_prompt":
            prompt = f"""{base_prompt}\n{self.hard_prompt}\n{additional_description}"""
        else:
            raise ValueError(f"Prompt {prompt_type} not found.")
        
        if self.args.feedback_discovered_categories:
            if self.scramble_known_intents:
                random.shuffle(self.known_intents)
            if self.args.semantic_feedback_discovery:
                self.current_sfd = self.get_semantic_feedback_discovery()
            else:
                feedback_discovery = self.known_intents
              
            if self.args.semantic_feedback_discovery_after_test_sample:
                prompt += """Don't discover new intents if you have already discovered an intent that is similar to the one you are trying to create. After every sample in TEST EXAMPLES, I will give you some known intents that are semantically similar to the one you are trying to predict, but you are encouraged to create a new intent if none the intents fit with the utterance. You must decide if reusing one of those intents or create a new one."""
            else:
                if self.args.semantic_feedback_discovery:
                    prompt += """Don't discover a new intent if you have already discovered one that is similar. The following is a list of some of the discovered intents so far, that can be semantically similar to the examples you are trying to predict: Try to reuse the intents as much as possible, with the objective of having the least amount of intents possible."""

                    feedback_discovery = []
                    for intent in self.current_sfd:
                        feedback_discovery.append(intent)
                    feedback_discovery = list(set(feedback_discovery))

                else:
                    prompt += """Don't discover a new intent if you have already discovered one that is similar. Make sure that the intents are not very generic, you can be fine-grained. Use the following list of known intents to keep reference, reuse them as much as possible: """

                discovered_categories = ', '.join([f"{intent}" for intent in feedback_discovery])

                # Add discovered categories to the prompt
                prompt += discovered_categories

        return prompt

    def get_semantic_feedback_discovery(self):
        # Compute embeddings of test samples
        test_samples_embeddings = []
        for sample in self.current_test_samples:
            utterance = sample[1]
            embedding = self.test_intents_embeddings.get_embedding(utterance)
            test_samples_embeddings.append(embedding)

        # Extract K nearest neighbors of known intents for each test sample
        distances, indices = self.known_intents_embeddings.get_k_nearest(test_samples_embeddings, self.args.k_nearest_feedback) 

        feedback_discovery = []
        for i in range(len(indices)):
            for j, ind in enumerate(indices[i]):
                feedback_discovery.append((self.known_intents[ind], distances[i][j]))
        
        # Keep only unique sentences, updating the distance if the current one is smaller    
        unique_examples = {}
        for intent, distance in feedback_discovery:
            if intent not in unique_examples:
                unique_examples[intent] = (intent, distance)
            else:
                # Update if current distance is smaller
                if distance < unique_examples[intent][1]:
                    unique_examples[intent] = (intent, distance)
        
        # Sort by distance and keep the top k examples
        feedback_discovery = sorted(unique_examples.items(), key=lambda item: item[1][1])[:self.args.number_semantic_feedback]
        feedback_discovery = [intent for intent, distance in feedback_discovery]
        return feedback_discovery

    # Method removed - now using LangChain LLMChain directly with local model
    
    def generate_with_retry(self, few_shot_examples, test_examples, prompt, expected_count, batch_id, max_retries=3):
        """
        带重试机制的LLM生成函数
        
        Args:
            few_shot_examples: Few-shot示例文本
            test_examples: 测试样本文本
            prompt: Prompt文本
            expected_count: 期望的输出数量
            batch_id: 批次ID(用于日志)
            max_retries: 最大重试次数
            
        Returns:
            tuple: (raw_output, parsed_output) 或 (None, None)如果所有重试失败
        """
        for attempt in range(max_retries):
            try:
                # 尝试生成
                output = self.completion_with_backoff(
                    few_shot_examples=few_shot_examples,
                    test_examples=test_examples,
                    prompt=prompt
                )
                
                # 解析输出
                parsed_output = parse_output(output)
                
                # 验证解析结果
                if parsed_output and len(parsed_output) == expected_count:
                    if attempt > 0:
                        print(f" Batch {batch_id}: Retry {attempt} succeeded!")
                    return output, parsed_output
                
                # 解析失败或数量不匹配
                if attempt < max_retries - 1:
                    print(f"  Batch {batch_id}: Attempt {attempt + 1} failed - Expected {expected_count}, got {len(parsed_output) if parsed_output else 0}")
                    print(f" Retrying with enhanced format emphasis...")
                    
                    # 增强格式强调 - 在prompt前添加更强的格式要求
                    format_emphasis = """
 CRITICAL FORMAT REQUIREMENT 

You MUST output EXACTLY {count} results in valid JSON array format.
DO NOT include any explanations, headers, or extra text.
ONLY output the JSON array with {count} objects.

Required format:
[
  {{"id": 0, "utterance": "...", "intent": "..."}},
  {{"id": 1, "utterance": "...", "intent": "..."}}
]

 OUTPUT ONLY JSON - NO OTHER TEXT 

""".format(count=expected_count)
                    prompt = format_emphasis + prompt
                    
                    # 短暂延迟后重试
                    time.sleep(0.5)
                else:
                    print(f" Batch {batch_id}: All {max_retries} attempts failed")
                    return output, parsed_output  # 返回最后一次尝试的结果
                    
            except Exception as e:
                print(f" Batch {batch_id}: Attempt {attempt + 1} error: {str(e)}")
                if attempt < max_retries - 1:
                    print(f" Retrying after error...")
                    time.sleep(0.5)
                else:
                    print(f" Batch {batch_id}: All retries exhausted due to errors")
                    return None, None
        
        return None, None
            
    def run(self):
        self.final_output = []
        failed_batches = []  # Track failed batches for diagnostics
        retry_stats = {'total_retries': 0, 'successful_retries': 0}  # Track retry statistics
        total_batches = len(self.data.test_dataloader)
        
        print(f"\n{'='*60}")
        print(f" Starting Intent Discovery Run (with Retry Mechanism)")
        print(f"{'='*60}")
        print(f"Total batches: {total_batches}")
        print(f"Batch size: {self.args.batch_size}")
        print(f"Max retries per batch: 3")
        print(f"Results path: {self.results_path}\n")
        
        for i, batch in tqdm(enumerate(self.data.test_dataloader), desc="Processing batches", total=total_batches):
            try:
                b_size = batch['input_ids'].shape[0]

                test_examples, gt_labels, _ = self.get_test_samples(i, batch, b_size, return_formatted=False)
                self.current_test_samples, self.current_gt_labels = test_examples, gt_labels
                
                if self.scramble_test_samples:
                    # Randomize test samples and labels
                    randomized_samples = list(zip(self.current_test_samples, self.current_gt_labels))
                    random.shuffle(randomized_samples)
                    self.current_test_samples, self.current_gt_labels = zip(*randomized_samples)
                
                if self.args.sample_few_shot_every_iteration:
                    few_shot_examples = self.get_few_shot_examples()
                else:
                    few_shot_examples = self.current_few_shot_samples
                
                prompt = self.get_prompt()
                if self.args.semantic_feedback_discovery_after_test_sample and self.args.semantic_feedback_discovery:
                    test_examples = '\n'.join([f"ID:{id}, Utterance: {utterance}, Similar Intents: {', '.join(self.current_sfd[k])}" for k, (id, utterance) in enumerate(test_examples)])
                else:
                    test_examples = '\n'.join([f"ID:{id},  Utterance: {utterance}" for id, utterance in test_examples])
                                
                if self.args.verbose:
                    print(self.long_prompt.format(few_shot_examples=few_shot_examples, test_examples=test_examples, prompt=prompt))
                    print(f" Prompt length: {len(self.long_prompt.format(few_shot_examples=few_shot_examples, test_examples=test_examples, prompt=prompt))}")
                
                # 使用带重试机制的生成函数
                output, parsed_output = self.generate_with_retry(
                    few_shot_examples=few_shot_examples,
                    test_examples=test_examples,
                    prompt=prompt,
                    expected_count=len(gt_labels),
                    batch_id=i+1
                )
                
                # 如果重试后仍然失败
                if output is None or parsed_output is None:
                    print(f"\n Batch {i+1}/{total_batches}: Failed after all retries")
                    failed_batches.append({
                        'batch_id': i,
                        'expected': len(gt_labels),
                        'got': 0,
                        'error': 'All retries failed'
                    })
                    continue
                
                if self.attack:
                    parsed_output = self.attack_samples(parsed_output)

                if self.args.feedback_discovered_categories:
                    discovered_categories = list(set([item['intent'] for item in parsed_output]))
                    for intent in discovered_categories:
                        if intent not in self.known_intents:
                            self.known_intents.append(intent)
                            if self.args.semantic_feedback_discovery: # Add the discovered embedding to the database
                                self.known_intents_embeddings.get_embedding(intent)

                    # Update the file that stores the discovered categories (DB)
                    with open(self.args.discovered_categories_path, 'w') as f:
                        for item in self.known_intents:
                            f.write(f"{item}\n")

                # Append labels to parsed output
                # Handle cases where parsing still failed after retries
                if len(parsed_output) != len(gt_labels):
                    print(f"\n  Batch {i+1}/{total_batches}: Mismatch after retries - Expected {len(gt_labels)}, got {len(parsed_output)}")
                    print(f" Raw output preview (first 500 chars):\n{output[:500]}...")
                    
                    # Record failed batch
                    failed_batches.append({
                        'batch_id': i,
                        'expected': len(gt_labels),
                        'got': len(parsed_output),
                        'raw_output': output,
                        'reason': 'Mismatch after retries'
                    })
                    
                    # Try to save partial results if any were parsed
                    if len(parsed_output) > 0:
                        print(f"  Saving {len(parsed_output)} successfully parsed items...")
                        retry_stats['successful_retries'] += 1
                        for k in range(min(len(parsed_output), len(gt_labels))):
                            parsed_output[k]['gt_label'] = gt_labels[k]
                            self.final_output.append(parsed_output[k])
                    
                    continue
                
                for k in range(len(parsed_output)):
                    parsed_output[k]['gt_label'] = gt_labels[k]
                    if self.args.verbose:
                        print(f"Predicted sample {parsed_output[k]['id']}: {parsed_output[k]['utterance']}, {parsed_output[k]['intent']}, {parsed_output[k]['gt_label']}")
                    self.final_output.append(parsed_output[k])

                # Save gt intents in a txt
                gt_intents = [item['gt_label'] for item in self.final_output]
                gt_intents = list(set(gt_intents))
                with open(os.path.join(self.results_path, 'gt_intents.txt'), 'w') as f:
                    for item in gt_intents:
                        f.write(f"{item}\n")
                        
            except Exception as e:
                print(f"\n Error processing batch {i+1}/{total_batches}: {str(e)}")
                import traceback
                traceback.print_exc()
                failed_batches.append({
                    'batch_id': i,
                    'error': str(e),
                    'traceback': traceback.format_exc()
                })
                continue
        
        # Save diagnostics if there were failures
        if failed_batches:
            import json
            failed_path = os.path.join(self.results_path, 'failed_batches.json')
            print(f"\n  {len(failed_batches)}/{total_batches} batches failed during processing")
            print(f" Saving diagnostics to: {failed_path}")
            with open(failed_path, 'w') as f:
                json.dump(failed_batches, f, indent=2)
        
        # Print final statistics with retry information
        print(f"\n{'='*60}")
        print(f" Run Completed (with Retry Mechanism)")
        print(f"{'='*60}")
        print(f"Successfully processed: {len(self.final_output)} samples")
        print(f"Total batches: {total_batches}")
        print(f"Failed batches: {len(failed_batches)}")
        print(f"Successful retries: {retry_stats['successful_retries']}")
        print(f"Batch success rate: {((total_batches - len(failed_batches)) / total_batches * 100):.1f}%")
        if len(self.data.test_dataloader.dataset) > 0:
            print(f"Sample coverage: {(len(self.final_output) / len(self.data.test_dataloader.dataset) * 100):.1f}%")
        print(f"{'='*60}\n")

    @retry(wait=wait_random_exponential(min=2, max=200), stop=stop_after_attempt(100))
    def attack_with_backoff(self, **kwargs):
        return self.attack_llm.run(**kwargs)
    
    def clean_output(self, output):
        #lowercase 
        output = output.lower().strip()
        intent = output.replace('intent:', '')
        intent = intent.replace(' ', '_')
        return intent
    
    def attack_samples(self, parsed_output):
        attacked_output = []
        for i in range(len(parsed_output)):
            sample = parsed_output[i]
            if random.random() < self.attack_probability:
                output = self.attack_with_backoff(utterance=sample['utterance'])
                intent = self.clean_output(output)
                sample['intent'] = intent
            attacked_output.append(sample)
        return attacked_output
    
    def get_embeddings(self, text_list, embeddings_path):
        
        embeddings = None

        # Check if the embeddings have already been computed
        if os.path.exists(embeddings_path):
            with open(embeddings_path, 'rb') as handle:
                embedding_dict = pickle.load(handle)

            # Valiate if all embeddings are there
            try:
                embeddings = [embedding_dict[label] for label in text_list]
            except KeyError:
                embeddings = None
        
        if embeddings is None:
            unique_labels = list(set(text_list))
            embeddings = self.embedder.embed_texts(unique_labels)
            embedding_dict = {label: embedding for label, embedding in zip(unique_labels, embeddings)}
            with open(embeddings_path, 'wb') as handle:
                pickle.dump(embedding_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
            embeddings = [embedding_dict[label] for label in text_list]
        return embeddings
    
    def get_clusters(self, y_pred, y_true):
        if self.args.intent_embeddings:
            # Compute embeddings
            pred_embeddings_path = os.path.join(self.results_path, f'pred_embeddings_{self.args.intent_embeddings}.pickle')
            true_embeddings_path = os.path.join(os.path.dirname(self.results_path), f'true_embeddings_{self.args.intent_embeddings}.pickle')

            pred_embeddings = self.get_embeddings(y_pred, pred_embeddings_path)
            
            # Compute number of clusters using DBSCAN
            dbscan = DBSCAN(eps=self.args.eps_dbscan, min_samples=2)
            dbscan.fit(pred_embeddings)
            labels = dbscan.labels_
            num_clusters = len(set(labels)) - (1 if -1 in labels else 0)

            clustering_model_pred = KMeans(n_clusters=num_clusters)
            clustering_model_pred.fit(pred_embeddings)
            labels_pred = clustering_model_pred.labels_

            if self.args.use_embeddings_for_labels:
                true_embeddings = self.get_embeddings(y_true, true_embeddings_path)
                clustering_model_true = KMeans(n_clusters=num_clusters)
                clustering_model_true.fit(true_embeddings)
                labels_true = clustering_model_true.labels_
            else:
                label_encoder = LabelEncoder()
                labels_true = label_encoder.fit_transform(y_true)            

        else:
            label_encoder = LabelEncoder()
            labels_true = label_encoder.fit_transform(y_true)
            labels_pred = label_encoder.fit_transform(y_pred)
        return labels_pred, labels_true

    def validate(self, results_path = None):
        try:
            print(f"\n{'='*60}")
            print(f" Starting Validation")
            print(f"{'='*60}\n")
            
            if results_path is not None:
                # Post-validation mode: load existing results
                self.results_path = results_path
                results_csv_path = os.path.join(self.results_path, 'results.csv')
                
                if not os.path.exists(results_csv_path):
                    raise FileNotFoundError(f" results.csv not found at {results_csv_path}")
                
                print(f" Loading results from: {results_csv_path}")
                results = pd.read_csv(results_csv_path)
                y_true = results['gt_label'].tolist()
                y_pred = results['intent'].tolist()
                print(f" Loaded {len(y_true)} samples from existing results.csv")
                
            else:
                # Runtime validation mode: generate results.csv
                
                # Check if we have data to validate
                if not hasattr(self, 'final_output') or len(self.final_output) == 0:
                    print(" ERROR: No results to validate! self.final_output is empty.")
                    print("\n  This usually means:")
                    print("   1. run() method was not called")
                    print("   2. All batches failed during processing")
                    print("   3. LLM output parsing failed for all samples")
                    print("\n Troubleshooting:")
                    print("   - Check if 'failed_batches.json' exists in results directory")
                    print("   - Review batch processing logs above")
                    print("   - Ensure LLM is generating output in the expected format")
                    return None
                
                print(f" Validating {len(self.final_output)} samples...")
                
                # Generate results.csv
                df = pd.DataFrame(self.final_output)
                results_csv_path = os.path.join(self.results_path, 'results.csv')
                
                if os.path.exists(results_csv_path):
                    print(f"  Appending to existing results.csv")
                    df.to_csv(results_csv_path, mode='a', index=False, header=False)
                else:
                    print(f" Creating new results.csv at:\n   {results_csv_path}")
                    df.to_csv(results_csv_path, index=False)
                
                print(f" results.csv saved successfully!")
                
                # Extract labels
                y_true = [item['gt_label'] for item in self.final_output]
                y_pred = [item['intent'] for item in self.final_output]
            
            # Validate data consistency
            if len(y_true) != len(y_pred):
                print(f"  Warning: Mismatch between y_true ({len(y_true)}) and y_pred ({len(y_pred)})")
            
            print(f"\n Computing clustering metrics...")
            print(f"   - Number of samples: {len(y_pred)}")
            print(f"   - Unique true intents: {len(set(y_true))}")
            print(f"   - Unique predicted intents: {len(set(y_pred))}")
            
            # Compute clustering scores
            labels_true, labels_pred = self.get_clusters(
                y_pred=y_pred,
                y_true=y_true
            )
            res = clustering_score(labels_true, labels_pred)

            # Add metadata
            res['date'] = self.timestamp
            res['id'] = self.exp_id
            res['readable_id'] = self.readable_id
            res['num_discovered_intents'] = len(set(y_pred))
            res['num_true_intents'] = len(set(y_true))
            res['num_samples'] = len(y_pred)
            res['results_path'] = self.results_path

            # Generate scores.csv
            df = pd.DataFrame([res])
            scores_csv_path = os.path.join(self.results_path, 'scores.csv')
            
            if os.path.exists(scores_csv_path):
                print(f"  Appending to existing scores.csv")
                df.to_csv(scores_csv_path, mode='a', index=False, header=False)
            else:
                print(f" Creating new scores.csv at:\n   {scores_csv_path}")
                df.to_csv(scores_csv_path, index=False)
            
            print(f" scores.csv saved successfully!")
            
            # Print evaluation results
            print(f"\n{'='*60}")
            print(f" Evaluation Results")
            print(f"{'='*60}")
            print(f"NMI Score:           {res['NMI']:.2f}%")
            print(f"ARI Score:           {res['ARI']:.2f}%")
            print(f"ACC Score:           {res['ACC']:.2f}%")
            print(f"Discovered Intents:  {res['num_discovered_intents']}")
            print(f"True Intents:        {res['num_true_intents']}")
            print(f"Total Samples:       {res['num_samples']}")
            print(f"{'='*60}\n")
            
            # Interpret results
            print(" Performance Assessment:")
            if res['NMI'] > 75 and res['ARI'] > 70 and res['ACC'] > 75:
                print("    EXCELLENT - High-quality intent discovery!")
            elif res['NMI'] > 60 and res['ARI'] > 55 and res['ACC'] > 60:
                print("    GOOD - Acceptable intent discovery performance")
            else:
                print("     NEEDS IMPROVEMENT - Consider adjusting:")
                print("      - Temperature parameter (try 0.1-0.2)")
                print("      - Few-shot examples (increase quantity or improve quality)")
                print("      - Prompt design (try hard_prompt strategy)")
            print()

            # Report in wandb
            if self.args.use_wandb:
                wandb.log(res)
            
            return res
            
        except Exception as e:
            print(f"\n Error in validate(): {str(e)}")
            import traceback
            traceback.print_exc()
            print("\n Troubleshooting:")
            print("   - Ensure run() completed successfully")
            print("   - Check that self.final_output contains data")
            print("   - Verify results directory has write permissions")
            raise

    def get_data(self):
        data = MyData(self.args, self.args)
        print(f"Number of test samples: {data.test_dataloader.dataset.__len__()}")
        return data
