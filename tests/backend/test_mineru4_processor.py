"""MinerU 4.x 处理器（engine=mineru4）单元测试。

用假的 ``mineru`` 模块替换真实 4.x，验证：
* 入参/返回契约与其它引擎一致；
* 页范围、ocr_mode 换算；
* 表格输出为 JSON 代码块；
* 页眉/页脚按同一套规则补回；
* 版本不符（装了 2.x）时给出明确错误而不是 ImportError。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import types

import pytest

from algorithm.preproc.preproc_src.processors import mineru4_processor
from algorithm.preproc.preproc_src.processors.mineru4_processor import Mineru4Processor

# 假的 4.0 结果：第 1 页封面（带页眉/页脚），第 2 页正文
FAKE_MARKDOWN = (
    "# 中 华 人 民 共 和 国 国 家 标 准\n\n"
    "GB/T48000.4—2026\n\n"
    "# 标准数字化 第4部分:协同制定要求\n\n"
    "2026-01-28发布\n\n"
    "国 家 市 场 监 督 管 理 总 局\n\n"
    "## 1 范围\n\n"
    "本文件规定了标准协同制定活动中的通用要求。\n"
)
FAKE_STRUCTURED = {
    "pages": [
        {
            "page_idx": 0,
            "blocks": [
                {"type": "header", "bbox": [0.1, 0.03, 0.2, 0.06], "content": "ICS01.120 CCS A00"},
                {"type": "doc_title", "bbox": [0.1, 0.13, 0.9, 0.17], "content": "中 华 人 民 共 和 国 国 家 标 准"},
            ],
        },
        {
            "page_idx": 1,
            "blocks": [
                {"type": "title", "bbox": [0.1, 0.1, 0.3, 0.2], "content": "1 范围"},
            ],
        },
    ]
}


class _FakeResult:
    def __init__(self, markdown: str, structured: dict):
        self._markdown = markdown
        self._structured = structured

    def markdown(self, **_kwargs) -> str:
        return self._markdown

    def structured_content(self, **_kwargs) -> dict:
        return self._structured


@pytest.fixture()
def pdf_file(tmp_path):
    """process() 会先校验文件存在，测试用一个真实存在的占位 PDF。"""
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"%PDF-1.4 test")
    return str(path)


@pytest.fixture()
def fake_mineru(monkeypatch):
    """注入假 mineru 4.0 模块，并记录调用参数。"""
    calls: list[dict] = []

    def parse(path, *, tier="standard", ocr_mode="auto", page_range="", **kwargs):
        calls.append({"path": path, "tier": tier, "ocr_mode": ocr_mode, "page_range": page_range})
        return _FakeResult(FAKE_MARKDOWN, FAKE_STRUCTURED)

    module = types.ModuleType("mineru")
    module.parse = parse
    module.__version__ = "4.0.4"
    monkeypatch.setitem(sys.modules, "mineru", module)
    # Exercise the real worker argument parser and output files across a mocked
    # process boundary; subprocess isolation itself is covered separately below.
    from algorithm.preproc.preproc_src.processors import mineru4_worker

    def run_worker(cmd, **kwargs):
        return types.SimpleNamespace(returncode=mineru4_worker.main(cmd[2:]), stderr="")

    monkeypatch.setattr(mineru4_processor.subprocess, "run", run_worker)

    monkeypatch.setattr(
        mineru4_processor, "extract_edge_lines",
        lambda *_a, **_k: {"top": ["ICS01.120", "CCS A00"], "bottom": ["2026-01-28发布 2026-08-01实施"]},
    )
    return calls


def _fake_version(monkeypatch, value: str) -> None:
    import importlib.metadata as metadata

    monkeypatch.setattr(metadata, "version", lambda _name: value)


# ------------------------------------------------------------------ 契约

def test_process_returns_same_contract_as_other_engines(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    result = Mineru4Processor().process(pdf_file, ocr_mode="auto", start_page_id=0, end_page_id=None,
                                        keep_page_edges="off")
    assert set(result) == {"content", "metadata"}
    assert isinstance(result["content"], str) and result["content"]
    meta = result["metadata"]
    assert meta["processor"] == "mineru4"
    assert meta["engine"] == "mineru4"
    assert meta["tables_json"] == {"count": 0, "forms": 0}
    assert "error" not in meta


def test_missing_file_reports_error_in_metadata(tmp_path):
    result = Mineru4Processor().process(str(tmp_path / "nope.pdf"), keep_page_edges="off")
    assert result["content"] == ""
    assert "文件不存在" in result["metadata"]["error"]


def test_subprocess_mode_is_recorded(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    result = Mineru4Processor().process(pdf_file, keep_page_edges="off")
    assert result["metadata"]["mineru4_options"]["mode"] == "subprocess"
    assert result["metadata"]["mineru4_options"]["version"] == "4.0.4"


def test_subprocess_preserves_parse_options(pdf_file, fake_mineru, monkeypatch):
    """统一主环境的子进程解析保留调用参数。"""
    _fake_version(monkeypatch, "4.0.4")
    calls: list[dict] = []

    def fake_subprocess(path, *, tier, ocr_mode, page_range):
        calls.append({"tier": tier, "ocr_mode": ocr_mode, "page_range": page_range})
        return FAKE_MARKDOWN, FAKE_STRUCTURED, {"mineru_version": "4.0.4", "elapsed": 1.0}

    monkeypatch.setattr(Mineru4Processor, "_run_in_subprocess", staticmethod(fake_subprocess))
    result = Mineru4Processor().process(
        pdf_file, ocr_mode="off", start_page_id=0, end_page_id=2, keep_page_edges="off"
    )
    assert calls == [{"tier": "standard", "ocr_mode": "off", "page_range": "1-3"}]
    options = result["metadata"]["mineru4_options"]
    assert options["mode"] == "subprocess" and options["version"] == "4.0.4"
    assert "中 华 人 民 共 和 国 国 家 标 准" in result["content"]


@pytest.mark.parametrize("major, expected", [(None, False), (2, False), (4, True), (5, False)])
def test_mineru_availability_uses_main_environment_only(tmp_path, monkeypatch, major, expected):
    interpreter = tmp_path / "legacy-python"
    interpreter.touch()
    monkeypatch.setenv("MINERU4_PYTHON", str(interpreter))
    monkeypatch.setattr(mineru4_processor, "mineru_major_version", lambda: major)
    processor = _real_main_processor().DocumentProcessor(output_dir=str(tmp_path / "out"))
    assert processor._local_mineru4_available() is expected


# -------------------------------------------------------------- 参数换算

@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(0, None, ""), (0, 0, "1"), (0, 2, "1-3"), (5, 7, "6-8"), (5, None, "6")],
)
def test_page_range_is_converted_to_one_based(start, end, expected):
    assert Mineru4Processor._page_range(start, end) == expected


@pytest.mark.parametrize(("ours", "theirs"), [("auto", "auto"), ("on", "ocr"), ("off", "txt")])
def test_ocr_mode_mapping(pdf_file, fake_mineru, monkeypatch, ours, theirs):
    _fake_version(monkeypatch, "4.0.4")
    Mineru4Processor().process(pdf_file, ocr_mode=ours, start_page_id=3, end_page_id=5, keep_page_edges="off")
    assert fake_mineru[-1]["ocr_mode"] == theirs
    assert fake_mineru[-1]["page_range"] == "4-6"


def test_unsupported_options_are_recorded_not_silently_swallowed(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    result = Mineru4Processor().process(
        pdf_file, lang="ch", table_enable=True, formula_enable=False, keep_page_edges="off"
    )
    ignored = result["metadata"]["mineru4_options"]["ignored_options"]
    assert ignored["lang"] == "ch" and ignored["formula_enable"] is False


def test_tier_defaults_to_standard_and_env_overrides(pdf_file, monkeypatch, fake_mineru):
    _fake_version(monkeypatch, "4.0.4")
    Mineru4Processor().process(pdf_file, keep_page_edges="off")
    assert fake_mineru[-1]["tier"] == "standard"
    monkeypatch.setenv("MINERU4_TIER", "basic")
    Mineru4Processor().process(pdf_file, keep_page_edges="off")
    assert fake_mineru[-1]["tier"] == "basic"


# ---------------------------------------------------------- 输出格式

def test_tables_become_json_blocks(pdf_file, monkeypatch, fake_mineru):
    _fake_version(monkeypatch, "4.0.4")
    html = (
        "<table><tr><td>角色</td><td>权限</td></tr>"
        "<tr><td>甲</td><td>查看、编辑</td></tr></table>"
    )
    monkeypatch.setattr(
        sys.modules["mineru"], "parse",
        lambda *a, **k: _FakeResult(f"# 标题\n\n{html}\n", FAKE_STRUCTURED),
    )
    result = Mineru4Processor().process(pdf_file, keep_page_edges="off")
    assert "<table" not in result["content"]
    assert result["metadata"]["tables_json"] == {"count": 1, "forms": 0}
    block = result["content"].split("```jsonl\n")[1].split("```")[0]
    assert [json.loads(line) for line in block.splitlines() if line.strip()] == [{"角色": "甲", "权限": "查看、编辑"}]


def test_sparse_table_is_rendered_as_key_value_json(pdf_file, monkeypatch, fake_mineru):
    """稀疏表（收文处理单）→ 键值对 JSON，不套 columns/rows。"""
    _fake_version(monkeypatch, "4.0.4")
    html = (
        "<table><tr><td>收文号:</td><td colspan=2>信息公司收(2026)209号</td></tr>"
        "<tr><td>来文单位:</td><td colspan=2>中国中车集团有限公司</td></tr>"
        "<tr><td>领导批示:</td><td colspan=2>请阅办。白雪峰 2026-03-27 16:36</td></tr></table>"
    )
    monkeypatch.setattr(
        sys.modules["mineru"], "parse",
        lambda *a, **k: _FakeResult(f"# 收文处理单\n\n{html}\n", FAKE_STRUCTURED),
    )
    result = Mineru4Processor().process(pdf_file, keep_page_edges="off")

    assert "<table" not in result["content"]
    assert result["metadata"]["tables_json"] == {"count": 0, "forms": 1}
    document = json.loads(result["content"].split("```json")[1].split("```")[0])
    assert document["doc_type"] == "form"
    assert document["title"] == "收文处理单"
    assert document["fields"]["收文号"] == "信息公司收(2026)209号"
    assert document["signatures"][0]["signer"] == "白雪峰"


def test_keep_page_edges_first_page_injects_top_and_bottom(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    result = Mineru4Processor().process(pdf_file, keep_page_edges="first_page")
    content = result["content"]
    injected = result["metadata"]["page_edges"]["injected"]
    assert [item["page"] for item in injected] == [1]
    # 页眉在封面之前、页脚在封面之后、正文之前
    assert content.index("ICS01.120") < content.index("中 华 人 民 共 和 国 国 家 标 准")
    assert content.index("2026-01-28发布 2026-08-01实施") < content.index("## 1 范围")


def test_keep_page_edges_off_adds_nothing(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    result = Mineru4Processor().process(pdf_file, keep_page_edges="off")
    assert "ICS01.120" not in result["content"]
    assert result["metadata"]["page_edges"]["injected"] == []


def test_existing_edge_text_is_not_duplicated(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    monkeypatch.setattr(
        mineru4_processor, "extract_edge_lines",
        lambda *_a, **_k: {"top": ["GB/T48000.4—2026"], "bottom": []},
    )
    result = Mineru4Processor().process(pdf_file, keep_page_edges="first_page")
    assert result["metadata"]["page_edges"]["injected"] == []
    assert result["content"].count("GB/T48000.4—2026") == 1


def _real_main_processor():
    """按文件加载真实的 main_processor。

    conftest 里的 session 级 fixture 会把 ``algorithm.preproc.preproc_src.main_processor``
    替换成桩模块，直接 import 会拿到假实现；这里按路径加载真实模块（显式设置 __package__
    以便模块内的相对导入正常工作）。
    """
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "algorithm/preproc/preproc_src/main_processor.py"
    spec = importlib.util.spec_from_file_location(
        "algorithm.preproc.preproc_src._real_main_processor", path
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "algorithm.preproc.preproc_src"
    spec.loader.exec_module(module)
    return module


def test_engine_is_registered_for_pdf_and_office():
    DocumentProcessor = _real_main_processor().DocumentProcessor

    assert "mineru4" in DocumentProcessor.ENGINE_MATRIX[".pdf"]
    assert "mineru4" in DocumentProcessor.ENGINE_MATRIX[".docx"]
    # 不改变既有引擎与默认选路
    assert DocumentProcessor.ENGINE_MATRIX[".pdf"][:3] == ("docling", "vlm", "mineru")


def test_explicit_selection_and_force_are_supported(tmp_path):
    DocumentProcessor = _real_main_processor().DocumentProcessor

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-test")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))
    engine, selection = processor.select_engine(str(sample), requested="mineru4", force=True)
    assert engine == "mineru4"
    assert selection["mode"] == "forced"


# ------------------------------------------------ 非 PDF 格式（4.0 仅 flash 档）

@pytest.mark.parametrize("suffix", ["docx", "doc", "xlsx", "xls", "csv", "ofd", "pptx", "html"])
def test_non_pdf_formats_run_at_flash_tier(pdf_file, fake_mineru, monkeypatch, suffix):
    _fake_version(monkeypatch, "4.0.4")
    sample = Path(pdf_file).with_suffix(f".{suffix}")
    sample.write_bytes(b"x")
    result = Mineru4Processor().process(str(sample), keep_page_edges="first_page")
    assert fake_mineru[-1]["tier"] == "flash"
    assert fake_mineru[-1]["page_range"] == ""          # 4.0 只对 PDF 支持页范围
    options = result["metadata"]["mineru4_options"]
    assert options["tier"] == "flash" and "flash" in options["tier_reason"]
    assert options["file_suffix"] == suffix
    # 非 PDF 不做页边补回（依赖 PDF 文本层与分页锚点）
    assert result["metadata"]["page_edges"]["injected"] == []


def test_pdf_keeps_requested_tier_and_page_range(pdf_file, fake_mineru, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    Mineru4Processor(tier="advanced").process(pdf_file, start_page_id=0, end_page_id=2, keep_page_edges="off")
    assert fake_mineru[-1]["tier"] == "advanced"
    assert fake_mineru[-1]["page_range"] == "1-3"


def test_engine_name_is_reflected_in_metadata(pdf_file, fake_mineru, monkeypatch):
    """engine=mineru / vlm 复用 4.x 适配层时，元数据仍如实反映请求的引擎名。"""
    _fake_version(monkeypatch, "4.0.4")
    for name in ("mineru", "vlm", "mineru4"):
        result = Mineru4Processor(engine_name=name).process(pdf_file, keep_page_edges="off")
        assert result["metadata"]["engine"] == name
        assert result["metadata"]["processor"] == "mineru4"


def test_mineru_major_version_helper(monkeypatch):
    monkeypatch.setattr(mineru4_processor, "mineru_major_version", lambda: 4)
    assert mineru4_processor.mineru_major_version() == 4


# ------------------------------------------------ 2.x 退役：引擎映射与多格式链路

def test_legacy_engine_names_are_backed_by_mineru4(tmp_path, monkeypatch):
    """装了 4.x 时，engine=mineru/vlm 直接由 4.x 适配层承担（2.x 退役）。"""
    monkeypatch.setattr(mineru4_processor, "mineru_major_version", lambda: 4)
    processor = _real_main_processor().DocumentProcessor(output_dir=str(tmp_path / "out"))
    for name in ("mineru", "vlm"):
        instance = processor._processor(name)
        assert type(instance).__name__ == "Mineru4Processor"
        assert instance.engine_name == name


def test_legacy_engine_never_falls_back_to_2x_processor(tmp_path, monkeypatch):
    """MinerU 2.x 已退役：主解释器里是 2.x 时也只用 4.x 适配层明确报错。"""
    monkeypatch.setattr(mineru4_processor, "mineru_major_version", lambda: 2)

    class _LegacyOCR:
        def __init__(self, **_kwargs):
            pass

    legacy = types.ModuleType("algorithm.preproc.preproc_src.processors.ocr_processor")
    legacy.OCRProcessor = _LegacyOCR
    monkeypatch.setitem(
        sys.modules, "algorithm.preproc.preproc_src.processors.ocr_processor", legacy
    )
    processor = _real_main_processor().DocumentProcessor(output_dir=str(tmp_path / "out"))
    for name in ("mineru", "vlm"):
        instance = processor._processor(name)
        assert type(instance).__name__ == "Mineru4Processor"
        assert instance.engine_name == name


def test_spreadsheet_and_ofd_accept_mineru4_but_keep_existing_defaults():
    matrix = _real_main_processor().DocumentProcessor.ENGINE_MATRIX
    for suffix in (".xlsx", ".xls", ".csv", ".ofd", ".docx", ".doc", ".pptx", ".ppt"):
        assert "mineru4" in matrix[suffix], suffix
    # 默认引擎不变：表格仍优先 pandas，OFD 仍是原 PDF 路线
    for suffix in (".xlsx", ".xls", ".csv"):
        assert matrix[suffix][0] == "pandas"
    assert matrix[".ofd"][:3] == ("docling", "vlm", "mineru")


def test_ofd_conversion_failure_falls_back_to_mineru4(tmp_path, monkeypatch):
    """OFD 转 PDF 失败且环境有 4.x 时，改走 4.x 原生 OFD 解析而不是直接报错。"""
    DocumentProcessor = _real_main_processor().DocumentProcessor
    ofd = tmp_path / "sample.ofd"
    ofd.write_bytes(b"OFD-fake")
    processor = DocumentProcessor(output_dir=str(tmp_path / "out"))

    class _Converter:
        last_conversion_error = "ofd 转换器不可用"

        def convert_to_pdf(self, _path):
            return None

    monkeypatch.setattr(processor, "_ofd_converter", lambda: _Converter())
    monkeypatch.setattr(mineru4_processor, "mineru_major_version", lambda: 4)
    captured: dict = {}

    class _Recorder:
        def process(self, path, **kwargs):
            captured.update({"path": path, **kwargs})
            return {"content": "OFD 原生解析结果", "metadata": {"processor": "mineru4"}}

    monkeypatch.setattr(processor, "_processor", lambda _engine: _Recorder())
    result = processor.process_file(str(ofd), keep_page_edges="off")

    assert result["success"] is True
    assert captured["path"] == str(ofd)                       # 未经过 PDF
    assert result["metadata"]["engine_selection"]["request_role"] == "ofd_conversion_fallback"
    assert result["metadata"]["engine_selection"]["converted_file_type"] == ".ofd"


@pytest.mark.parametrize('mode,expected', [('auto','auto'),('on','ocr'),('off','txt')])
def test_subprocess_uses_current_python_and_canonical_ocr_mode(pdf_file, monkeypatch, tmp_path, mode, expected):
    monkeypatch.setenv('MINERU4_PYTHON', '/nonexistent/legacy/python')
    monkeypatch.setattr(mineru4_processor, 'mineru_major_version', lambda: 4)
    def child(cmd, **kwargs):
        assert cmd[0] == sys.executable
        assert cmd[cmd.index('--ocr-mode') + 1] == expected
        out = Path(cmd[cmd.index('--out-dir') + 1])
        (out/'result.json').write_text(json.dumps({'ok': True, 'mineru_version': '4.0.4'}))
        (out/'markdown.md').write_text('识别正文')
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(mineru4_processor.subprocess, 'run', child)
    result = Mineru4Processor().process(pdf_file, ocr_mode=mode, keep_page_edges='off')
    assert result['content'] == '识别正文'


def test_native_child_failure_is_returned_as_parse_error(pdf_file, monkeypatch):
    monkeypatch.setattr(mineru4_processor.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(returncode=-11, stderr='native crash'))
    result = Mineru4Processor().process(pdf_file, keep_page_edges='off')
    assert result['content'] == ''
    assert 'native crash' in result['metadata']['error']


def test_worker_omits_image_bytes_but_keeps_ocr_text(tmp_path, monkeypatch):
    _fake_version(monkeypatch, "4.0.4")
    from algorithm.preproc.preproc_src.processors import mineru4_worker
    class Result:
        def markdown(self, *, image_renderer):
            return '印章文字\n' + image_renderer('data:image/png;base64,AAAA') + '\n截止日期2026年4月10日'
        def structured_content(self):
            return {'pages': []}
    monkeypatch.setitem(sys.modules, 'mineru', types.SimpleNamespace(parse=lambda *a, **k: Result(), __version__='4.0.4'))
    out=tmp_path/'out'
    assert mineru4_worker.main(['--pdf','fake.pdf','--out-dir',str(out),'--ocr-mode','ocr']) == 0
    text=(out/'markdown.md').read_text()
    assert 'base64' not in text
    assert '印章文字' in text and '截止日期2026年4月10日' in text


@pytest.mark.parametrize("version", ["2.7.6", "5.0.0", ""])
def test_worker_rejects_unsupported_mineru_version(tmp_path, monkeypatch, version):
    from algorithm.preproc.preproc_src.processors import mineru4_worker
    calls = []
    monkeypatch.setitem(sys.modules, "mineru", types.SimpleNamespace(parse=lambda *a, **k: calls.append(1)))
    _fake_version(monkeypatch, version)
    out = tmp_path / "worker"
    assert mineru4_worker.main(["--pdf", "fake.pdf", "--out-dir", str(out)]) == 1
    report = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert "需要 MinerU 4.x" in report["error"]
    assert calls == []


def test_worker_preserves_actual_parse_failure(tmp_path, monkeypatch):
    from algorithm.preproc.preproc_src.processors import mineru4_worker
    def fail(*args, **kwargs):
        raise RuntimeError("model load failed")
    monkeypatch.setitem(sys.modules, "mineru", types.SimpleNamespace(parse=fail))
    _fake_version(monkeypatch, "4.0.4")
    out = tmp_path / "worker"
    assert mineru4_worker.main(["--pdf", "fake.pdf", "--out-dir", str(out)]) == 1
    report = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert report["error"] == "RuntimeError: model load failed"
