"""Sanity probe for the frozen relation harness plus a mounted NanoJev checkpoint.

CI does not execute this against a live model.  Operators run it after
``docker/download_models.py --jev`` and before ``KG_EXTRACT_JEV_ENABLED=1``.

  .venv/bin/python -m algorithm.knowledge_extract.v2.jev_probe
"""
from __future__ import annotations

from .config import get_jev_config
from .jev_client import create_jev_client, jev_enabled
from .jev_harness import partition_relation_pairs


_PROBES = (
    {
        "id": "official.accept",
        "chunk": "现将《关于做好知识地图建设工作的通知》转发给你们，请遵照执行。",
        "source": "通知",
        "target": "知识地图建设工作",
        "relation": "FORWARDS",
        "expect": "gray_or_accept",
    },
    {
        "id": "official.reject",
        "chunk": "办公室不得将未审批事项对外单位发布。",
        "source": "办公室",
        "target": "外单位",
        "relation": "AUTHORIZES",
        "expect": "reject",
    },
    {
        "id": "standard.accept",
        "chunk": "本标准适用于铁路通信系统的术语定义。术语“应答器”属于通信设备。",
        "source": "应答器",
        "target": "通信设备",
        "relation": "BELONGS_TO",
        "expect": "accept",
    },
)


def main() -> int:
    config = get_jev_config()
    print(f"enabled={config['enabled']} model_path={config['model_path']}")
    print(f"accept={config['accept']} reject={config['reject']}")
    if not jev_enabled():
        print("KG_EXTRACT_JEV_ENABLED 未打开；仍会尝试加载模型以便探针。")
    try:
        client = create_jev_client(model_path=str(config["model_path"]))
    except Exception as exc:  # noqa: BLE001 - probe must stay runnable without the model
        print(f"无法加载 NanoJev，跳过探针: {exc}")
        return 2
    failures = 0
    for item in _PROBES:
        pair = {
            "pair_id": item["id"],
            "source_name": item["source"],
            "source_type": "Entity",
            "target_name": item["target"],
            "target_type": "Entity",
            "relation_id": item["relation"],
        }
        accepted, rejected, gray = partition_relation_pairs(
            [pair],
            chunk_text=item["chunk"],
            client=client,
        )
        chosen = accepted or rejected or gray
        judgment = (chosen[0].get("judgment") if chosen else {}) or {}
        print(
            f"{item['id']}: decision={judgment.get('decision')} "
            f"choice={judgment.get('choice')} confidence={judgment.get('confidence')} "
            f"window={judgment.get('window')!r} expect={item['expect']}"
        )
        decision = judgment.get("decision")
        if item["expect"] == "reject" and decision == "accept":
            failures += 1
        if item["expect"] == "accept" and decision == "reject":
            failures += 1
    if failures:
        print(f"探针与期望方向不一致: {failures} 条。保持 KG_EXTRACT_JEV_ENABLED=0。")
        return 1
    print("探针未出现方向性冲突。仍需用 v2_benchmark_eval 的公文/标准 case 看关系 P/R 后再打开。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
