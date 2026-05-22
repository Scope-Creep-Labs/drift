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


class LiveChartTrace(BaseModel):
    name: str
    promql: str
    unit: str | None = None


class LiveChartBlock(BaseModel):
    """Chart that re-runs its PromQL on a timer in the frontend. The
    block carries no data on emission — the LiveChart component polls
    /api/query each tick. `chart_key` is the replace-in-place identity:
    a later emission with the same key updates the existing chart
    (preserving Plotly zoom/hover) instead of creating a new one."""

    type: Literal["live_chart"] = "live_chart"
    chart_key: str
    title: str | None = None
    traces: list[LiveChartTrace]
    refresh_ms: int = 5000
    range_seconds: int = 600
    step_seconds: int = 15


class TerminalActionBlock(BaseModel):
    """A clickable card the agent emits when it wants the user to open
    a remote terminal to a device. The card stays in the conversation
    history; clicking it opens the existing terminal modal (same flow
    as the sidebar device row). The agent does NOT pre-create a session
    — that happens when the user clicks, so abandoned suggestions
    don't accumulate orphaned `pending` rows."""

    type: Literal["terminal_action"] = "terminal_action"
    device_name: str
    reason: str | None = None


RenderBlock = (
    MarkdownBlock
    | ChartBlock
    | TableBlock
    | MetricBlock
    | TimelineBlock
    | LiveChartBlock
    | TerminalActionBlock
)
