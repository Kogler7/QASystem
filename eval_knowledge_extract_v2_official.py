#!/usr/bin/env python3
from eval_knowledge_extract_v2_lib import main

if __name__ == "__main__":
    # official/docs 已全部替换为 md，main 默认走 local-direct：
    # 不启动后端，直接调用 algorithm/knowledge_extract_v2 算法。
    raise SystemExit(main("official"))
