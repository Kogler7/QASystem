"""Chinese sanity probe for the local Jev classifier.

Run only when the model is mounted.  CI does not execute this against a live
encoder; it prints noul scores for a few 公文/标准 snippets so operators can
see whether the distribution is usable before setting KG_EXTRACT_JEV_ENABLED=1.

  .venv/bin/python -m algorithm.knowledge_extract.v2.jev_probe
"""
from __future__ import annotations

from .config import get_jev_config
from .jev_client import create_jev_client, jev_enabled


_PROBES = (
    {
        "id": "official.accept",
        "chunk": "现将《关于做好知识地图建设工作的通知》转发给你们，请遵照执行。",
        "source": "通知",
        "target": "知识地图建设工作",
        "relation": "forwards",
        "expect": "gray_or_accept",
    },
    {
        "id": "official.reject",
        "chunk": "不得将未审批事项对外发布。",
        "source": "办公室",
        "target": "外单位",
        "relation": "authorizes",
        "expect": "reject",
    },
    {
        "id": "standard.accept",
        "chunk": "本标准适用于铁路通信系统的术语定义。术语“应答器”属于通信设备。",
        "source": "应答器",
        "target": "通信设备",
        "relation": "belongs_to",
        "expect": "accept",
    },
)


def main() -> int:
    config = get_jev_config()
    print(f"enabled={config['enabled']} model_path={config['model_path']}")
    print(
        "noul_accept={noul_accept} noul_reject={noul_reject}".format(**config)
    )
    if not jev_enabled():
        print("KG_EXTRACT_JEV_ENABLED 未打开；仍会尝试加载模型以便探针。")
    try:
        client = create_jev_client(model_path=str(config["model_path"]))
    except Exception as exc:  # noqa: BLE001 - probe must stay runnable without the model
        print(f"无法加载 Jev 模型，跳过探针: {exc}")
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
        from .jev_client import classify_relation_pair

        judgment = classify_relation_pair(client, chunk_text=item["chunk"], pair=pair)
        print(
            f"{item['id']}: p_yes={judgment['p_yes']:.3f} "
            f"decision={judgment['decision']} expect={item['expect']}"
        )
        if item["expect"] == "reject" and judgment["decision"] == "accept":
            failures += 1
        if item["expect"] == "accept" and judgment["decision"] == "reject":
            failures += 1
    if failures:
        print(f"探针与期望方向不一致: {failures} 条。保持 KG_EXTRACT_JEV_ENABLED=0。")
        return 1
    print("探针未出现方向性冲突。仍需用 v2_benchmark_eval 的公文/标准 case 看 P/R。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
