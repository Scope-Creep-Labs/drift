from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TimeRange(BaseModel):
    start: str
    end: str


class PromptContext(BaseModel):
    # Accept both camelCase (from the TS frontend) and snake_case wire keys.
    # The frontend has sent camelCase since day one; these aliases stop the
    # context from being silently ignored.
    model_config = ConfigDict(populate_by_name=True)

    asset_id: str | None = Field(default=None, alias="assetId")
    time_range: TimeRange | None = Field(default=None, alias="timeRange")
    investigation_id: str | None = Field(default=None, alias="investigationId")


class PromptRequest(BaseModel):
    prompt: str
    context: PromptContext | None = None


# RenderBlock variants (mirror frontend src/types/blocks.ts)


class MarkdownBlock(BaseModel):
    type: Literal["markdown"] = "markdown"
    content: str


class ChartBlock(BaseModel):
    type: Literal["chart"] = "chart"
    renderer: Literal["plotly", "echarts"] = "plotly"
    spec: dict[str, Any] = Field(default_factory=dict)
    dataRef: str | None = None
    title: str | None = None


class TableBlock(BaseModel):
    type: Literal["table"] = "table"
    columns: list[str]
    rows: list[list[Any]]
    title: str | None = None


class MetricBlock(BaseModel):
    type: Literal["metric"] = "metric"
    label: str
    value: float | int | str
    unit: str | None = None
    trend: Literal["up", "down", "flat"] | None = None


class TimelineEvent(BaseModel):
    ts: str
    label: str
    severity: Literal["info", "warn", "error"] | None = None


class TimelineBlock(BaseModel):
    type: Literal["timeline"] = "timeline"
    events: list[TimelineEvent]
    title: str | None = None


RenderBlock = MarkdownBlock | ChartBlock | TableBlock | MetricBlock | TimelineBlock
