import os
from pathlib import Path
from typing import Any

from mcp_services.runtime import mcp


def _executor():
    from algorithm.visualization.charts import VisualizationExecutor

    default_dir = Path.cwd() / "agentcode" / "image_result"
    return VisualizationExecutor(os.getenv("MCP_IMAGE_OUTPUT_DIR", str(default_dir)))


def _draw(method: str, data: dict[str, Any]) -> dict[str, Any]:
    return getattr(_executor(), method)(data)


@mcp.tool(name="draw_bar_chart")
def draw_bar_chart(labels: list[str], values: list[float], title: str = "", xlabel: str = "", ylabel: str = "", colors: list[str] | None = None) -> dict[str, Any]:
    """Generate a bar chart."""
    return _draw("bar_chart", locals())


@mcp.tool(name="draw_pie_chart")
def draw_pie_chart(labels: list[str], values: list[float], title: str = "", explode: list[float] | None = None) -> dict[str, Any]:
    """Generate a pie chart."""
    return _draw("pie_chart", locals())


@mcp.tool(name="draw_line_chart")
def draw_line_chart(x_data: list[Any], y_series: list[list[float]], title: str = "", xlabel: str = "", ylabel: str = "", legends: list[str] | None = None) -> dict[str, Any]:
    """Generate a line chart."""
    return _draw("line_chart", locals())


@mcp.tool(name="draw_scatter_plot")
def draw_scatter_plot(x_data: list[float], y_data: list[float], title: str = "", xlabel: str = "", ylabel: str = "", colors: str | list[str] = "#1f77b4", sizes: int | list[int] = 100) -> dict[str, Any]:
    """Generate a scatter plot."""
    return _draw("scatter_plot", locals())


@mcp.tool(name="draw_histogram")
def draw_histogram(values: list[float], title: str = "", xlabel: str = "", ylabel: str = "", bins: int = 10) -> dict[str, Any]:
    """Generate a histogram."""
    return _draw("histogram", locals())


@mcp.tool(name="draw_area_chart")
def draw_area_chart(x_data: list[Any], y_series: list[list[float]], title: str = "", xlabel: str = "", ylabel: str = "", legends: list[str] | None = None) -> dict[str, Any]:
    """Generate an area chart."""
    return _draw("area_chart", locals())


@mcp.tool(name="draw_radar_chart")
def draw_radar_chart(categories: list[str], values: list[list[float]], title: str = "", legends: list[str] | None = None) -> dict[str, Any]:
    """Generate a radar chart."""
    return _draw("radar_chart", locals())


@mcp.tool(name="draw_box_plot")
def draw_box_plot(data_sets: list[list[float]], labels: list[str] | None = None, title: str = "", ylabel: str = "") -> dict[str, Any]:
    """Generate a box plot."""
    return _draw("box_plot", locals())


@mcp.tool(name="draw_bubble_chart")
def draw_bubble_chart(x_data: list[float], y_data: list[float], sizes: list[float], title: str = "", xlabel: str = "", ylabel: str = "", colors: str | list[str] = "#1f77b4") -> dict[str, Any]:
    """Generate a bubble chart."""
    return _draw("bubble_chart", locals())


@mcp.tool(name="draw_heatmap")
def draw_heatmap(matrix: list[list[float]], x_labels: list[str] | None = None, y_labels: list[str] | None = None, title: str = "", xlabel: str = "", ylabel: str = "") -> dict[str, Any]:
    """Generate a heatmap."""
    return _draw("heatmap", locals())


@mcp.tool(name="list_chart_types")
def list_chart_types() -> dict[str, Any]:
    """List supported chart types."""
    descriptions = {
        "bar": "柱状图",
        "pie": "饼图",
        "line": "折线图",
        "scatter": "散点图",
        "histogram": "直方图",
        "area": "面积图",
        "radar": "雷达图",
        "box": "箱线图",
        "bubble": "气泡图",
        "heatmap": "热力图",
    }
    return {"chart_types": list(descriptions), "descriptions": descriptions}
