"""NanoJev uses the bounded SystemOne transport and a single warmed predictor."""
from docker.configuration import getenv as config_getenv
import gc
import importlib.util
import json
import os
import sys
from pathlib import Path

from clef_service.app import InputTooLong, ModelOutOfMemory, Settings, create_app


class NanoRuntime:
    def __init__(self, settings):
        self.settings = settings

    def load(self):
        if self.settings.dtype not in {"bfloat16", "float32"}:
            raise ValueError("NanoJev requires bfloat16 or float32")
        root = Path(self.settings.model_dir)
        script = root / "source/scripts/predict_toy_decisions.py"
        if not script.is_file():
            raise RuntimeError("NANOJEV_MODEL_DIR must contain a complete local checkpoint")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        sys.dont_write_bytecode = True
        from docker.model_services import selected_native, physical_gpu
        selected = selected_native('nanojev')
        if selected is not None:
            os.environ['CUDA_VISIBLE_DEVICES'] = physical_gpu(selected)
            os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
        import torch
        self.torch = torch
        from docker.model_services import selected_native
        record = selected_native('nanojev')
        torch.set_num_threads(int((record or {}).get('engine_options', {}).get('cpu_threads', '4')))
        spec = importlib.util.spec_from_file_location("_nanojev_predictor", script)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        self.predictor = self.module.DecisionPredictor(
            str(root), device_name=self.settings.device,
            precision="bf16" if self.settings.dtype == "bfloat16" else "fp32",
            max_length=self.settings.max_length, disable_native_triton=False,
        )
        self.infer({"model": "nanojev", "state": "The sky is blue.", "questions": {
            "color": {"type": "choice", "instructions": "What color is the sky?",
                      "criteria": {"blue": "Blue", "red": "Red"}}}})

    def infer(self, payload):
        questions = {}
        for key, question in payload["questions"].items():
            kind = question["type"]
            criteria = question.get("criteria")
            if kind == "noul":
                criteria = {"true": "True", "false": "False", **(criteria or {})}
            normalized = ({k: v or k for k, v in criteria.items()} if isinstance(criteria, dict)
                          else [v or str(i) for i, v in enumerate(criteria)])
            questions[key] = {"type": "boolean" if kind == "noul" else kind,
                              "instructions": question.get("instructions") or "Choose the best option.",
                              "criteria": normalized}
        state = payload["state"] if isinstance(payload["state"], str) else json.dumps(payload["state"], ensure_ascii=False)
        request = {"states": [{"id": "request", "state": state, "questions": questions}]}
        # The official encoder rejects oversized complete examples; do not truncate state.
        out_of_memory = False
        try:
            examples = self.module.prepare_examples(request, self.predictor.tokenizer, self.settings.max_length)
            result = self.predictor.predict(request)
        except self.torch.cuda.OutOfMemoryError:
            out_of_memory = True
        except ValueError as exc:
            if "超过 max_length=" in str(exc):
                raise InputTooLong() from None
            raise
        if out_of_memory:
            gc.collect()
            self.torch.cuda.empty_cache()
            raise ModelOutOfMemory() from None
        answers = {}
        for key, answer in result["states"][0]["answers"].items():
            probabilities = answer["probabilities"]
            kind = payload["questions"][key]["type"]
            if kind == "noul":
                answers[key] = {"type": "noul", "noul": probabilities["true"]}
            elif kind == "score":
                criteria = payload["questions"][key]["criteria"]
                answers[key] = {"type": "score", "score": sum(int(k)*v for k,v in probabilities.items()),
                                "confidence": max(probabilities.values()), "probabilities": probabilities,
                                "legend": {str(i): v for i,v in enumerate(criteria)}}
            else:
                answers[key] = {"type": "choice", "choice": answer["choice"],
                                "confidence": max(probabilities.values()), "probabilities": probabilities}
        # Count the longest candidate path, matching the per-example context limit.
        tokens = max(len(tokens) for example in examples for tokens in example["leaf_tokens"])
        return {"model": "nanojev", "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 0}}

    def close(self):
        if hasattr(self, "predictor"):
            del self.predictor
        gc.collect()
        if hasattr(self, "torch") and self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()


app = create_app(Settings.from_env("NANOJEV_"), NanoRuntime, model_name="nanojev")
