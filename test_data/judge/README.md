# Judge 校准数据

`v1` 当前数据版本为 1.1.1。2026-10-13 只把排除列表统一为 LF 并更新该文件的字节指纹，JSON 内容、排除条目和标签未改。1.1.0 的排除列表指纹来自 Windows CRLF 工作区，而 Git 已保存 LF，导致干净检出失败。

`.gitattributes` 固定本目录、verifier-scope-v1 和 judge_windows 的 LF。冻结请求与候选不重新生成，哈希测试保留；不能通过更新 manifest 来接受未经审核的数据内容修改。标签与来源校验由 `eval/judge_dataset.py` 执行。
