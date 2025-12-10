import yaml
from edc.edc_framework import EDC
import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["DASHSCOPE_API_KEY"] = ""


def load_config(path="config/config.yaml"):
    with open(path, "r", encoding="utf8") as f:
        return yaml.safe_load(f)

if __name__ == "__main__":
    cfg = load_config()
    edc = EDC(cfg)
    output = edc.extract_kg()
