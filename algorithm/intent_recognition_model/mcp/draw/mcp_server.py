from fastmcp.server import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from .executor import VisualizationExecutor

app = FastMCP(
    name="Data Visualization MCP",
    version="1.0.0"
)


def _normalize_request(data: Union[Dict, BaseModel], model_class) -> BaseModel:
    if isinstance(data, BaseModel):
        return data
    if isinstance(data, dict):
        if "request" in data and isinstance(data["request"], dict):
            return model_class(**data["request"])
        return model_class(**data)


class ChartResponse(BaseModel):
    success: bool = Field(..., description="Whether the chart was generated successfully")
    image_base64: str = Field(..., description="Base64 encoded PNG image of the chart")
    image_path: str = Field("", description="File path of the generated chart image (can be opened by IDE)")
    filename: str = Field("", description="Filename of the generated image")
    chart_type: str = Field(..., description="Type of chart generated")
    error: str = Field("", description="Error message if failed")


class BarChartRequest(BaseModel):
    labels: List[str] = Field(..., description="Labels for the X axis")
    values: List[float] = Field(..., description="Values for each bar")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")
    colors: Optional[List[str]] = Field(None, description="Custom colors for bars")


@app.tool(
    name="draw_bar_chart",
    description="""Generate a bar chart from data. Use this to compare categorical data across different groups.

Required parameters:
- labels: List of string labels for X-axis (e.g., ["Q1", "Q2", "Q3", "Q4"])
- values: List of numbers for each bar (must have same length as labels)

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- colors: Custom colors for bars (hex codes like ["#ff0000", "#00ff00"])

Example:
  labels = ["苹果", "香蕉", "橙子"], values = [10, 20, 15]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_bar_chart(request: Union[BarChartRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, BarChartRequest)
        executor = VisualizationExecutor()
        result = executor.bar_chart({
            'labels': req.labels,
            'values': req.values,
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel,
            'colors': req.colors
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="bar", error=str(e))


class PieChartRequest(BaseModel):
    labels: List[str] = Field(..., description="Labels for each pie slice")
    values: List[float] = Field(..., description="Values for each slice")
    title: Optional[str] = Field("", description="Chart title")
    explode: Optional[List[float]] = Field(None, description="Explode offsets for each slice")


@app.tool(
    name="draw_pie_chart",
    description="""Generate a pie chart from data. Use this to show proportions of a whole.

Required parameters:
- labels: List of string labels for each slice (e.g., ["A", "B", "C", "D"])
- values: List of numbers (positive values only) for each slice (must have same length as labels)

Optional parameters:
- title: Chart title text
- explode: List of float values (0-1) to offset slices (e.g., [0.1, 0, 0, 0])

Example:
  labels = ["销售", "管理", "研发"], values = [40, 25, 35]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_pie_chart(request: Union[PieChartRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, PieChartRequest)
        executor = VisualizationExecutor()
        result = executor.pie_chart({
            'labels': req.labels,
            'values': req.values,
            'title': req.title,
            'explode': req.explode or []
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="pie", error=str(e))


class LineChartRequest(BaseModel):
    x_data: List[Any] = Field(..., description="X axis data points")
    y_series: List[List[float]] = Field(..., description="Multiple Y data series")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")
    legends: Optional[List[str]] = Field(None, description="Legends for each series")


@app.tool(
    name="draw_line_chart",
    description="""Generate a line chart from data. Use this to show trends over time or continuous data.

Required parameters:
- x_data: List of X-axis values (e.g., ["Jan", "Feb", "Mar", "Apr"])
- y_series: List of Y data series (each series is a list of numbers with same length as x_data)

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- legends: List of legend names for each series (e.g., ["2023", "2024"])

Example:
  x_data = ["1月", "2月", "3月"], y_series = [[10, 20, 15], [15, 25, 20]]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_line_chart(request: Union[LineChartRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, LineChartRequest)
        executor = VisualizationExecutor()
        result = executor.line_chart({
            'x_data': req.x_data,
            'y_series': req.y_series,
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel,
            'legends': req.legends or []
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="line", error=str(e))


class ScatterPlotRequest(BaseModel):
    x_data: List[float] = Field(..., description="X axis data points")
    y_data: List[float] = Field(..., description="Y axis data points")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")
    colors: Optional[Union[str, List[str]]] = Field("#1f77b4", description="Point colors")
    sizes: Optional[Union[int, List[int]]] = Field(100, description="Point sizes")


@app.tool(
    name="draw_scatter_plot",
    description="""Generate a scatter plot from data. Use this to show correlation between two variables.

Required parameters:
- x_data: List of numeric X values (e.g., [1, 2, 3, 4, 5])
- y_data: List of numeric Y values (must have same length as x_data)

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- colors: Color(s) for points (single color or list of colors)
- sizes: Size(s) for points (single size or list of sizes)

Example:
  x_data = [1, 2, 3, 4, 5], y_data = [2, 4, 5, 7, 8]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_scatter_plot(request: Union[ScatterPlotRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, ScatterPlotRequest)
        executor = VisualizationExecutor()
        result = executor.scatter_plot({
            'x_data': req.x_data,
            'y_data': req.y_data,
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel,
            'colors': req.colors,
            'sizes': req.sizes
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="scatter", error=str(e))


class HistogramRequest(BaseModel):
    values: List[float] = Field(..., description="Data values to histogram")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")
    bins: Optional[int] = Field(10, description="Number of bins")


@app.tool(
    name="draw_histogram",
    description="""Generate a histogram from data. Use this to show distribution of numerical data.

Required parameters:
- values: List of numeric values (e.g., [1.2, 3.5, 2.7, 4.1, 5.3])

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- bins: Number of bins (default: 10)

Example:
  values = [1, 2, 2, 3, 3, 3, 4, 4, 5], bins = 5
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_histogram(request: Union[HistogramRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, HistogramRequest)
        executor = VisualizationExecutor()
        result = executor.histogram({
            'values': req.values,
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel,
            'bins': req.bins
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="histogram", error=str(e))


class AreaChartRequest(BaseModel):
    x_data: List[Any] = Field(..., description="X axis data points")
    y_series: List[List[float]] = Field(..., description="Multiple Y data series")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")
    legends: Optional[List[str]] = Field(None, description="Legends for each series")


@app.tool(
    name="draw_area_chart",
    description="""Generate an area chart from data. Use this to show cumulative totals over time.

Required parameters:
- x_data: List of X-axis values (e.g., ["Jan", "Feb", "Mar", "Apr"])
- y_series: List of Y data series (each series is a list of numbers with same length as x_data)

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- legends: List of legend names for each series

Example:
  x_data = ["Q1", "Q2", "Q3", "Q4"], y_series = [[100, 150, 120, 200], [80, 120, 100, 150]]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_area_chart(request: Union[AreaChartRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, AreaChartRequest)
        executor = VisualizationExecutor()
        result = executor.area_chart({
            'x_data': req.x_data,
            'y_series': req.y_series,
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel,
            'legends': req.legends or []
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="area", error=str(e))


class RadarChartRequest(BaseModel):
    categories: List[str] = Field(..., description="Categories for each axis")
    values: List[List[float]] = Field(..., description="Values for each category across datasets")
    title: Optional[str] = Field("", description="Chart title")
    legends: Optional[List[str]] = Field(None, description="Legends for each dataset")


@app.tool(
    name="draw_radar_chart",
    description="""Generate a radar chart from data. Use this to compare multiple items across multiple dimensions.

Required parameters:
- categories: List of dimension names (e.g., ["速度", "力量", "敏捷", "智力"])
- values: List of data sets (each set is a list of values, one per category)

Optional parameters:
- title: Chart title text
- legends: List of legend names for each data set

Example:
  categories = ["A", "B", "C", "D"], values = [[8, 7, 9, 6], [5, 8, 7, 9]]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_radar_chart(request: Union[RadarChartRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, RadarChartRequest)
        executor = VisualizationExecutor()
        result = executor.radar_chart({
            'categories': req.categories,
            'values': req.values,
            'title': req.title,
            'legends': req.legends or []
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="radar", error=str(e))


class BoxPlotRequest(BaseModel):
    data_sets: List[List[float]] = Field(..., description="Multiple datasets to compare")
    labels: Optional[List[str]] = Field(None, description="Labels for each dataset")
    title: Optional[str] = Field("", description="Chart title")
    ylabel: Optional[str] = Field("", description="Y axis label")


@app.tool(
    name="draw_box_plot",
    description="""Generate a box plot from data. Use this to show distribution and outliers across groups.

Required parameters:
- data_sets: List of data sets (each set is a list of numbers to compare)

Optional parameters:
- title: Chart title text
- ylabel: Y-axis label text
- labels: Labels for each data set

Example:
  data_sets = [[1,2,3,4,5], [2,3,4,5,6], [0,1,2,3,4]], labels = ["Group1", "Group2", "Group3"]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_box_plot(request: Union[BoxPlotRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, BoxPlotRequest)
        executor = VisualizationExecutor()
        result = executor.box_plot({
            'data_sets': req.data_sets,
            'labels': req.labels or [],
            'title': req.title,
            'ylabel': req.ylabel
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="box", error=str(e))


class BubbleChartRequest(BaseModel):
    x_data: List[float] = Field(..., description="X axis data points")
    y_data: List[float] = Field(..., description="Y axis data points")
    sizes: List[float] = Field(..., description="Sizes representing a third variable")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")
    colors: Optional[Union[str, List[str]]] = Field("#1f77b4", description="Bubble colors")


@app.tool(
    name="draw_bubble_chart",
    description="""Generate a bubble chart from data. Use this to show relationship between three variables.

Required parameters:
- x_data: List of numeric X values
- y_data: List of numeric Y values (same length as x_data)
- sizes: List of bubble sizes (same length as x_data, values > 0)

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- colors: Color(s) for bubbles

Example:
  x_data = [1, 2, 3, 4], y_data = [10, 20, 15, 25], sizes = [50, 100, 75, 125]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_bubble_chart(request: Union[BubbleChartRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, BubbleChartRequest)
        executor = VisualizationExecutor()
        result = executor.bubble_chart({
            'x_data': req.x_data,
            'y_data': req.y_data,
            'sizes': req.sizes,
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel,
            'colors': req.colors
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="bubble", error=str(e))


class HeatmapRequest(BaseModel):
    matrix: List[List[float]] = Field(..., description="2D matrix of values")
    x_labels: Optional[List[str]] = Field(None, description="X axis labels")
    y_labels: Optional[List[str]] = Field(None, description="Y axis labels")
    title: Optional[str] = Field("", description="Chart title")
    xlabel: Optional[str] = Field("", description="X axis label")
    ylabel: Optional[str] = Field("", description="Y axis label")


@app.tool(
    name="draw_heatmap",
    description="""Generate a heatmap from data. Use this to show correlation matrix or 2D data intensity.

Required parameters:
- matrix: 2D list of numbers (rows x columns)

Optional parameters:
- title: Chart title text
- xlabel: X-axis label text
- ylabel: Y-axis label text
- x_labels: Labels for X-axis columns
- y_labels: Labels for Y-axis rows

Example:
  matrix = [[1, 0.5, 0.2], [0.5, 1, 0.8], [0.2, 0.8, 1]], x_labels = ["A", "B", "C"]
""",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def draw_heatmap(request: Union[HeatmapRequest, Dict[str, Any]]) -> ChartResponse:
    try:
        req = _normalize_request(request, HeatmapRequest)
        executor = VisualizationExecutor()
        result = executor.heatmap({
            'matrix': req.matrix,
            'x_labels': req.x_labels or [],
            'y_labels': req.y_labels or [],
            'title': req.title,
            'xlabel': req.xlabel,
            'ylabel': req.ylabel
        })
        return ChartResponse(**result)
    except Exception as e:
        return ChartResponse(success=False, image_base64="", image_path="", filename="", chart_type="heatmap", error=str(e))


class ListChartTypesResponse(BaseModel):
    chart_types: List[str] = Field(..., description="List of supported chart types")
    descriptions: Dict[str, str] = Field(..., description="Description of each chart type")


@app.tool(
    name="list_chart_types",
    description="List all supported chart types and their descriptions",
    annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False}
)
def list_chart_types() -> ListChartTypesResponse:
    chart_info = {
        "bar": "柱状图 - 用于比较不同类别数据",
        "pie": "饼图 - 用于展示各部分占整体的比例",
        "line": "折线图 - 用于展示数据随时间的趋势变化",
        "scatter": "散点图 - 用于展示两个变量之间的相关性",
        "histogram": "直方图 - 用于展示数值数据的分布",
        "area": "面积图 - 用于展示累计数据随时间的变化",
        "radar": "雷达图 - 用于多维度数据比较",
        "box": "箱线图 - 用于展示数据分布和异常值",
        "bubble": "气泡图 - 用于展示三个变量之间的关系",
        "heatmap": "热力图 - 用于展示矩阵数据或相关性"
    }
    
    return ListChartTypesResponse(
        chart_types=list(chart_info.keys()),
        descriptions=chart_info
    )


if __name__ == "__main__":
    import sys
    import asyncio
    
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        print("🟢 启动数据可视化 MCP stdio 服务...")
        asyncio.run(app.run_stdio_async())
    else:
        print("🟢 启动数据可视化 MCP HTTP 服务...")
        print("📡 服务地址: http://0.0.0.0:8890")
        print("🔧 使用方式:")
        print("   - OpenAPI 文档: http://localhost:8890/docs")
        print("   - 启动 stdio 模式: python mcp_server.py stdio")
        asyncio.run(app.run_http_async(host="0.0.0.0", port=8890))