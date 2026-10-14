"""Extraction configuration and tracing on top of the shared LLM runtime."""
from __future__ import annotations
import importlib
from typing import Any
from qasystem_config.configuration import getenv as config_getenv
from external_services.llm.runtime import QuotaPool, ScheduledClient, get_pool


def create_request_llm(config: dict[str, Any]):
    version = config["version"]
    if version == "v3.1":
        package = "v3_1"
    elif version == "v2.1":
        package = "v2_1"
    elif version == "v2.2":
        package = "v2_2"
    elif version == "v2.2-v31":
        package = "v2_2_v31"
    else:
        package = version
    module = importlib.import_module(f"algorithm.knowledge_extract.{package}.llm_client")
    factory = module.create_llm_client
    credential = config_getenv(config["api_key_env"], "") if config.get('api_key_env') else ''
    if (config["provider"] != "default" or config.get('credential_required')) and not credential.strip():
        raise ValueError("Extraction provider credential is not configured")
    # A nonempty placeholder prevents algorithm factories from falling back
    # to an unrelated API credential for an unauthenticated local service.
    llm = factory(base_url=config["base_url"], api_key=credential or 'not-needed',
                  model=config["model"], max_tokens=config.get("max_output_tokens") or config["max_tokens"],
                  request_timeout=config["request_timeout"], disable_thinking=config["disable_thinking"],
                  **({"schedule_requests": False,
                      "semantic_thinking": config.get("semantic_thinking", False),
                      **{key: config[key] for key in ('stream_generation', 'verification_max_tokens', 'fast_verification')
                         if key in config},
                      "thinking_parameter": config.get("thinking_parameter", "thinking")}
                     if version == "v3.1" else {}))
    llm.client = ScheduledClient(llm.client, config)
    llm.api_mode = config.get("api_mode", "auto")
    llm.max_input_tokens = int(config.get("max_input_tokens", 65536))
    llm.max_output_tokens = int(config.get("max_output_tokens") or config["max_tokens"])
    llm.max_concurrency = max(1, int(config.get("max_concurrency", 4)))
    from backend.app.api_recording import create_extraction_trace
    trace = create_extraction_trace(config=config, secrets=(credential,))
    if trace is not None:
        llm.extraction_trace = trace
        llm.client.extraction_trace = llm.extraction_trace
    return llm
