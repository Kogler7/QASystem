import torch
from transformers import T5Tokenizer, T5ForConditionalGeneration

class PrunerClassifier:
    # LOWP: 模型文件夹路径待改
    def __init__(self, model_dir="./t5_pruner_best", device=None, max_length=64):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.max_length = max_length

        self.tokenizer = T5Tokenizer.from_pretrained(model_dir)
        self.model = T5ForConditionalGeneration.from_pretrained(model_dir).to(self.device)
        self.model.eval()

    def predict(self, text):
        """单条预测"""
        input_text = f"classify: {text}"
        enc = self.tokenizer(
            input_text,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_length
        ).to(self.device)

        with torch.no_grad():
            output = self.model.generate(
                input_ids=enc["input_ids"],
                attention_mask=enc["attention_mask"],
                max_length=4
            )

        pred = self.tokenizer.decode(output[0], skip_special_tokens=True)
        return pred

    def batch_predict(self, texts):
        """批量预测，返回列表"""
        input_texts = [f"classify: {t}" for t in texts]

        enc = self.tokenizer(
            input_texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_length
        ).to(self.device)

        with torch.no_grad():
            outputs = self.model.generate(
                input_ids=enc["input_ids"],
                attention_mask=enc["attention_mask"],
                max_length=4
            )

        preds = [self.tokenizer.decode(o, skip_special_tokens=True) for o in outputs]
        return preds
