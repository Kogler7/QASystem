# Introduction to LangChain Capabilities

import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set cache directories
os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
os.environ.setdefault("TORCH_HOME", os.path.expanduser("~/.cache/torch"))

from langchain_core.prompts import PromptTemplate
from langchain_classic.chains import LLMChain
from langchain.llms import OpenAI
# from langchain import OpenAI

from intentgpt.data.dataloaders import MyData

from argparse import Namespace
import re

from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)



def get_hard_examples_and_prompt(data, dataset_name='clinc', N=100):
    # Check if prompt exists
    hard_examples_path = f'prompts/{dataset_name}/hard_examples.txt'
    argumentation_path = f'prompts/{dataset_name}/argumentation.txt'
    guidelines_path = f'prompts/{dataset_name}/guidelines.txt'

    if os.path.exists(hard_examples_path) and os.path.exists(argumentation_path) and os.path.exists(guidelines_path):
        print("Prompt already exists.")
        # extract hard_samples, argumentation, guidelines
        with open(hard_examples_path, 'r') as f:
            hard_samples = f.read()
        with open(argumentation_path, 'r') as f:
            argumentation = f.read()
        with open(guidelines_path, 'r') as f:
            guidelines = f.read()
        return hard_samples, argumentation, guidelines
    
    else:
            
        print("Generating prompt...")

        # Dictionary of {class: [utterances]}
        classes = {}
        for sample in data.train_labeled_loader.dataset:
            # add sample to classes using label as key
            label = int(sample['labels'])
            if label not in classes:
                classes[label] = []
            utterance = data.tokenizer.decode(sample['input_ids'], skip_special_tokens = True)
            classes[label].append(utterance)
        
        # order classes by key
        classes = dict(sorted(classes.items()))

        # Define class id to class name mapping
        class_map = {v: k for k, v in data.train_label_map.items()}

        # select N samples per class
        N = 2
        dict_class_examples = {}
        for class_id, class_name in class_map.items():
            dict_class_examples[class_name] = classes[class_id][:N]

        N_few_shot = 10
        # Generate prompt
        prompt = '''You are a helpful assistant and and expert in natural language processing labeling. 
        You are an expert in the task of open set intent classification, where textual utterances need to be asigned to a certain intent/category that we may do not know a priori.
        I will give you a list of known intents and each intent has several utterances that are labeled with that intent. Your task is to study the utterances and intents, and understand how the intents are selected.
        I want you to write a list {N_few_shot} pairs of utterances and intents that you think are the most difficult to classify from the ones that I gave you. After that you should argue why you think these are the most difficult pairs to classify.
        
        Finally, You must also respond with a short and general tutorial and guidelines that you would give to a human annotator or another AI assistant (taking into account why you chose the pairs difficult to classify). 
        
        You must give the most difficult pairs of utterances and intents, and a comprehensive tutorial on how to solve task. Take into account that your response will directly be sent to the annotator for task description, so think step by step to respond with the best possible set of examples and guidelines.
            {train_examples}

            You must respond using the following format:

            Hard examples for few-shot intent classification:
            Utterance: <utterance>, Intent: <intent>

            Argumentation of why these were selected as hard examples:
            <argumentation>

            Guidelines for annotators:
            <guidelines>

            RESPONSE STARTS HERE
                '''
        
        # Each example is a string of the form "Utterance: <utterance>, Intent: <intent>"
        train_examples = []
        for class_name, utterances in dict_class_examples.items():
            for utterance in utterances:
                train_examples.append(f'Utterance: {utterance}, Intent: {class_name}')

        train_examples = '\n'.join(train_examples)

        long_prompt = PromptTemplate(template=prompt, input_variables=["train_examples", "N_few_shot"])
        llm_openai = OpenAI(model_name="gpt-4", temperature=1)
        llm_chain = LLMChain(prompt=long_prompt, llm=llm_openai)
        print(long_prompt.format(train_examples=train_examples, N_few_shot=N_few_shot))

        @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(2))
        def completion_with_backoff(**kwargs):
            return llm_chain.run(**kwargs)
        
        output = completion_with_backoff(train_examples=train_examples, N_few_shot=N_few_shot)

        # Extracting hard samples
        hard_samples = re.findall(r'Utterance: (.*?), Intent: (.*?)\n', output)

        # Extracting argumentation
        arg_start = output.find('These examples have been selected because')
        arg_end = output.find('Guidelines for annotators:')
        argumentation = output[arg_start:arg_end].strip()

        # Extracting guidelines
        guidelines_start = output.find('When analyzing a potential intent behind an utterance:')
        guidelines_end = output.find('Remember that the task\'s objective is to understand')
        guidelines = output[guidelines_start:guidelines_end].strip()

        # Save results in txt
        os.makedirs(os.path.dirname(hard_examples_path), exist_ok=True)
        with open(hard_examples_path, 'w') as f:
            f.write('\n'.join([f'Utterance: {utterance}, Intent: {intent}' for utterance, intent in hard_samples]))
        with open(argumentation_path, 'w') as f:
            f.write(argumentation)
        with open(guidelines_path, 'w') as f:
            f.write(guidelines)
        return hard_samples, argumentation, guidelines

def main():
    # Define dataset
    exp_dict = {
        'data_dir': 'data',
        'dataset': 'clinc',
        'seed': 7,
        'label_type': 'original',
        'known_cls_ratio': 0.25,
        'cluster_num_factor': 1.0,
        'labeled_ratio': 0.1,
        'model_name': 'bert-base-uncased',
        'max_seq_length': 512,
        'batch_size': 32,
    }

    # Convert exp_dict to a namespace
    args = Namespace(**exp_dict)
    data = MyData(args, exp_dict)

    # Generate prompt
    prompt, examples = get_hard_examples_and_prompt(data, dataset_name=args.dataset)
    print(prompt)
    # Generate res

if __name__ == "__main__":
    main()
