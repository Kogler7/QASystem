#!/usr/bin/env python3
"""在 MinerU 4.x 所在的解释器里执行一次解析（供适配层以子进程方式调用）。

模型加载和推理始终在子进程执行，避免原生代码阻塞 API worker 心跳。
始终复用主进程的 Python 解释器，不支持独立环境或解释器路径覆盖。

用法：

    python mineru4_worker.py \\
        --pdf /path/in.pdf --out-dir /tmp/xxx \\
        --tier standard --ocr-mode auto --page-range 1-3

产物（写入 --out-dir）：

* ``markdown.md``   —— 4.x 默认渲染的 Markdown
* ``structured.json`` —— ``ParseResult.structured_content()``（页/块/bbox，用于分页定位）
* ``result.json``   —— 版本、耗时等元信息

只需要 stdlib + mineru，因此不依赖仓库其它模块。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MinerU 4.x 单次解析 worker")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--tier", default="standard")
    parser.add_argument("--ocr-mode", default="auto", choices=["auto", "ocr", "txt"])
    parser.add_argument("--page-range", default="")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        import mineru
        from importlib.metadata import version as _dist_version

        try:
            mineru_version = _dist_version("mineru")
        except Exception:
            mineru_version = getattr(mineru, "__version__", "")

        if str(mineru_version).split(".", 1)[0] != "4":
            raise RuntimeError(
                f"当前 Python 环境需要 MinerU 4.x，实际版本为 {mineru_version or '未知'}；"
                "请使用包含 MinerU 4.x 的 qasystem-env-v1 环境镜像"
            )

        result = mineru.parse(
            args.pdf,
            tier=args.tier,
            ocr_mode=args.ocr_mode,
            page_range=args.page_range,
        )
        # Keep OCR text and captions, but never feed embedded image bytes to chunking/LLMs.
        markdown = result.markdown(image_renderer=lambda *_args, **_kwargs: "") or ""
        (out_dir / "markdown.md").write_text(markdown, encoding="utf-8")

        structured: dict = {}
        try:
            structured = result.structured_content() or {}
        except Exception as exc:  # 结构化结果只用于分页定位，拿不到就退化为整篇处理
            structured = {"error": str(exc)}
        (out_dir / "structured.json").write_text(
            json.dumps(structured, ensure_ascii=False), encoding="utf-8"
        )

        (out_dir / "result.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "mineru_version": mineru_version,
                    "tier": args.tier,
                    "ocr_mode": args.ocr_mode,
                    "page_range": args.page_range,
                    "elapsed": round(time.time() - started, 2),
                    "chars": len(markdown),
                    "pages": len(getattr(result, "pages", []) or []),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return 0
    except Exception as exc:  # 让主进程通过 result.json 拿到明确原因
        (out_dir / "result.json").write_text(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}", "elapsed": round(time.time() - started, 2)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"mineru4 worker failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
