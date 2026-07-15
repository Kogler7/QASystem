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
        "auto", "text", "docling", "vlm", "mineru", "pandas",
        "beautifulsoup", "excel", "bs4", "html",
    ] = "auto"
    mineru_options: MinerUOptions = Field(default_factory=MinerUOptions)


class ChunkOptions(StrictModel):
    strategy: Literal["recursive", "symbols", "custom_recursive", "markdown"] = "recursive"
    size: int = Field(default=500, gt=0)
    separators: list[str] | None = None
    similarity: float = Field(default=0.6, ge=0, le=1)

    def legacy_chunk_type(self) -> int:
        return {"recursive": 0, "symbols": 1, "custom_recursive": 2, "markdown": 3}[self.strategy]


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


class TextChunk(StrictModel):
    text: str
    ranges: list[int] | None = Field(default=None, min_length=2, max_length=2)

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


class GraphExtractionItem(StrictModel):
    text: str


class GraphItem(StrictModel):
    graph: dict[str, Any]


class ChunkRequest(StrictModel):
    file_name: str = "document.md"
    markdown: str = Field(min_length=1)
    options: ChunkOptions = Field(default_factory=ChunkOptions)


class GraphExtractionRequest(StrictModel):
    ontology: str = Field(min_length=1)
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
