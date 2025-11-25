import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Set cache directories (can be overridden by environment variables)
os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
os.environ.setdefault("TORCH_HOME", os.path.expanduser("~/.cache/torch"))

from omegaconf import OmegaConf
import random

from intentgpt import IntentGPT

def main(args):
    intentGPT = IntentGPT(args)
    if args.only_validate_results:
        intentGPT.validate(args.only_validate_results)
    else:
        intentGPT.run()
        intentGPT.validate()

if __name__ == "__main__":
    
    cli_conf = OmegaConf.from_cli()
    specific_conf = OmegaConf.load(cli_conf.pop('config')) if 'config' in cli_conf else {}
    exp_dict = OmegaConf.merge(specific_conf, cli_conf)
    random.seed(exp_dict.seed)
    main(exp_dict)
