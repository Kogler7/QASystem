from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MinerUOptions(StrictModel):
    language: str = Field(default="ch", min_length=1)
    ocr_mode: Literal["auto", "on", "off"] = "auto"
    table_enable: bool = True
    formula_enable: bool = True


class ParserConfig(StrictModel):
    engine: Literal[
        "auto", "text", "docling", "vlm", "mineru", "mineru4", "pandas",
        "beautifulsoup", "excel", "bs4", "html",
    ] = "auto"
    force: bool = False
    page_sharding: Literal["auto", "on", "off"] = "auto"
    page_shard_size: int = Field(default=0, ge=0, le=256)
    max_page_shards: int = Field(default=16, ge=1, le=32)
    mineru_options: MinerUOptions = Field(default_factory=MinerUOptions)
    # 保留页眉/页脚：解析器默认把页面上下边缘的文字当版面装饰丢弃，标准封面的
    # ICS/CCS、发布与实施日期正在其中。first_page 指本次解析范围内的首页。
    keep_page_edges: Literal["off", "first_page", "all"] | list[int] = "first_page"
    # 页边文本短于该长度时按水印/标志残片丢弃。
    page_edge_min_chars: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def validate_force(self):
        if self.force and self.engine == "auto":
            raise ValueError("force=true 时必须指定 engine")
        return self


class ChunkOptions(StrictModel):
    strategy: Literal["recursive", "symbols", "custom_recursive", "markdown", "semantic"] = "recursive"
    size: int = Field(default=4000, gt=0)
    separators: list[str] | None = None
    similarity: float = Field(default=0.6, ge=0, le=1)
    chunk_overlap: int = Field(default=0, ge=0)
    size_unit: Literal["char", "token"] = "char"
    breakpoint_method: Literal["percentile", "stddev", "none"] = "percentile"
    chunk_threshold: int = Field(default=12000, ge=0)

    @model_validator(mode="after")
    def validate_overlap_lt_size(self):
        if self.chunk_overlap >= self.size:
            raise ValueError("chunk_overlap 必须小于 size")
        return self

    def legacy_chunk_type(self) -> int:
        return {"recursive": 0, "symbols": 1, "custom_recursive": 2, "markdown": 3, "semantic": 4}[self.strategy]


class ExtractionOptions(StrictModel):
    model: str | None = None
    merge: bool = True


class StorageOptions(StrictModel):
    persist_graph: bool = False
    knowledge_space_id: str | None = None


class PipelineOptions(StrictModel):
    parser: ParserConfig = Field(default_factory=ParserConfig)
    chunking: ChunkOptions = Field(default_factory=ChunkOptions)
    extraction: ExtractionOptions = Field(default_factory=ExtractionOptions)
    storage: StorageOptions = Field(default_factory=StorageOptions)


class TextChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    ranges: list[int] | None = Field(default=None, min_length=2, max_length=2)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.ranges is not None:
            if any(value < 0 for value in self.ranges):
                raise ValueError("ranges 不能包含负数")
            if self.ranges[1] < self.ranges[0]:
                raise ValueError("ranges 结束位置不能小于开始位置")
        return self


class TextEmbeddingItem(StrictModel):
    text: str


class GraphExtractionItem(TextChunk):
    """Extraction input with optional source offsets and provenance metadata."""


class GraphItem(StrictModel):
    graph: dict[str, Any]


class ChunkRequest(StrictModel):
    file_name: str = "document.md"
    markdown: str = Field(min_length=1)
    options: ChunkOptions = Field(default_factory=ChunkOptions)


class GraphExtractionRequest(StrictModel):
    ontology: str = ""
    items: list[GraphExtractionItem] = Field(min_length=1)
    options: ExtractionOptions = Field(default_factory=ExtractionOptions)


class TextEmbeddingRequest(StrictModel):
    items: list[TextEmbeddingItem] = Field(min_length=1)


class GraphEmbeddingRequest(StrictModel):
    items: list[GraphItem] = Field(min_length=1)


class GraphMergeRequest(StrictModel):
    items: list[GraphItem] = Field(min_length=1)


class GraphRequest(BaseModel):
    graph: dict[str, Any] = Field(..., description="包含 nodes 和 relationships 的知识图谱数据")
