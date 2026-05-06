# Drift — AI-Enabled Agentic Observability Platform

## Product Spec — MVP

## Vision

**Drift** is an AI-enabled agentic observability platform for time-series systems.

It helps engineers investigate anomalies, regressions, optimization problems, and operational behavior across field assets, infrastructure, edge devices, and industrial systems.

Drift combines:
- observability data
- AI agents
- workflow engines
- time-series analytics
- anomaly detection
- optimization analysis
- dynamic visual investigation

The prompt-driven notebook is not the entire product.

It is one major UI component inside Drift.

---

# Positioning

## Primary Positioning

```text
Drift — Agentic observability for time-series systems
```

## Alternative Positioning

```text
Drift — AI-powered investigation workspace for operational systems
```

```text
Drift — Prompt-native observability for field assets and infrastructure
```

---

# Product Model

Drift is the platform.

The notebook is the investigation interface.

```text
Drift Platform
    ├── Agent runtime
    ├── Workflow orchestration
    ├── Observability integrations
    ├── Time-series analytics
    ├── Anomaly detection
    ├── Optimization engines
    ├── Investigation memory
    └── Investigation Notebook UI
```

---

# What the Notebook Does

The notebook provides a simple prompt-driven investigation surface.

Users type natural language prompts.

The backend engine analyzes telemetry, runs workflows, performs anomaly detection or optimization, and returns rich interactive responses.

The UI renders:
- markdown
- dynamic charts
- tables
- metrics
- timelines
- recommendations

The experience should feel like:

```text
ChatGPT + Grafana + Jupyter
```

without requiring users to write code.

---

# Core UX Principle

Keep the user-facing interface simple.

```text
Prompt → Rich Response
```

No explicit:
- markdown cells
- query cells
- workflow cells
- notebook programming model

Those may exist internally later, but the MVP should expose only prompt-driven investigations.

---

# Example Interaction

## User Prompt

```text
Why did gateway-17 become unstable yesterday afternoon?
```

## Drift Response

```text
Instability begins around 14:35 UTC following memory growth and repeated process restarts.
```

Rendered outputs:
- markdown explanation
- memory usage chart
- latency chart
- anomaly markers
- restart timeline
- recommendations

---

# Product Goals

## 1. Prompt-native observability

Users should type:

```text
Why did CPU usage spike after deployment?
```

instead of writing queries, scripts, or notebooks.

---

## 2. Agentic investigation

Drift should be able to:
- fetch relevant telemetry
- inspect related signals
- correlate events
- run anomaly detection
- compare assets
- analyze deployment regressions
- propose likely causes
- recommend next actions

---

## 3. Rich visual responses

Responses are interactive and visual.

Supported output types:
- markdown
- Plotly charts
- ECharts dashboards
- tables
- metrics
- timelines
- anomaly overlays
- recommendations

---

## 4. Time-series first

Designed primarily for:
- telemetry
- observability
- infrastructure
- edge devices
- industrial systems
- optimization systems
- IoT fleets
- energy systems
- field assets

---

## 5. Engine agnostic

The Drift UI should work with:
- Langflow
- n8n
- MCP tools
- Python agents
- custom APIs
- SQL backends
- observability systems

without changing the frontend.

---

# High-Level Architecture

```text
Drift Frontend
    ↓
Investigation Notebook UI
    ↓
Drift Runtime
    ↓
Engine Adapter Layer
    ↓
Execution Engines
    - Langflow
    - n8n
    - MCP tools
    - Custom AI backend
    - Python services
```

---

# Drift Platform Components

## 1. Investigation UI

Prompt-driven interface for engineers.

Primary interaction:

```text
Ask → Analyze → Render → Refine
```

---

## 2. Agent Runtime

Coordinates AI reasoning and tool execution.

Responsibilities:
- interpret user prompt
- choose data sources
- call tools/workflows
- request telemetry
- run analysis
- produce structured response blocks

---

## 3. Engine Adapter Layer

Allows Drift to use multiple backend engines.

Adapters:
- LangflowAdapter
- N8NAdapter
- MCPAdapter
- PythonAdapter
- CustomAPIAdapter

---

## 4. Observability Integrations

Connects to telemetry and event sources.

Examples:
- Prometheus
- InfluxDB
- TimescaleDB
- OpenSearch
- MQTT
- Kafka
- custom asset APIs

---

## 5. Analysis Layer

Performs:
- anomaly detection
- regression detection
- correlation analysis
- lag analysis
- forecast comparison
- optimization inspection
- root-cause exploration

---

# Response Rendering Model

Drift backend responses return structured render blocks.

Example:

```json
{
  "blocks": [
    {
      "type": "markdown",
      "content": "CPU usage spiked immediately after deployment."
    },
    {
      "type": "chart",
      "renderer": "plotly",
      "spec": {}
    },
    {
      "type": "markdown",
      "content": "Likely cause: memory leak in process worker-x."
    }
  ]
}
```

---

# Render Block Types

## Markdown Block

```ts
{
  type: "markdown";
  content: string;
}
```

---

## Chart Block

```ts
{
  type: "chart";
  renderer: "plotly" | "echarts";
  spec: object;
  dataRef?: string;
}
```

---

## Table Block

```ts
{
  type: "table";
  columns: string[];
  rows: any[][];
}
```

---

## Metric Block

```ts
{
  type: "metric";
  label: string;
  value: string | number;
  unit?: string;
}
```

---

## Timeline Block

```ts
{
  type: "timeline";
  events: TimelineEvent[];
}
```

---

# Charting Strategy

## Plotly First

Plotly should be the primary renderer for investigation notebooks because Drift targets observability and time-series analysis.

Best for:
- synchronized hover
- time zooming
- range sliders
- subplots
- annotations
- event overlays
- scatter analysis
- statistical plots
- anomaly investigation
- telemetry analysis
- optimization visualization
- regression detection

---

## ECharts Support

ECharts should be supported for:
- operational dashboards
- lightweight embedded views
- mobile responsiveness
- realtime streaming dashboards
- KPI monitoring

---

# Example Scenarios

## Scenario 1 — Device Instability

### Prompt

```text
Why did gateway-17 become unstable yesterday?
```

### Drift Actions

- Fetch telemetry
- Run anomaly detection
- Correlate metrics
- Detect restart spikes
- Generate summary

### Response

Rendered outputs:
- markdown summary
- memory usage chart
- latency chart
- restart events
- anomaly overlays
- recommendations

---

## Scenario 2 — Fleet-Wide Thermal Anomalies

### Prompt

```text
Show devices with abnormal thermal behavior this week.
```

### Drift Actions

- Query fleet telemetry
- Compare devices
- Rank outliers
- Identify thermal drift
- Summarize likely causes

### Response

Rendered outputs:
- anomaly ranking table
- thermal scatter plots
- fleet heatmap
- outlier timelines

---

## Scenario 3 — Optimization Analysis

### Prompt

```text
Find the optimal dispatch schedule while minimizing thermal stress.
```

### Drift Actions

- Load forecasts
- Read asset constraints
- Run optimization
- Compare baseline vs optimized schedule
- Identify tradeoffs

### Response

Rendered outputs:
- SOC trajectory chart
- dispatch chart
- thermal envelope
- constraint violations
- markdown recommendations

---

## Scenario 4 — Deployment Regression Detection

### Prompt

```text
Did software release v2.8 increase CPU usage?
```

### Drift Actions

- Identify deployment window
- Compare before/after telemetry
- Compute percentiles
- Detect regression
- Summarize impact

### Response

Rendered outputs:
- before/after comparison charts
- percentile distributions
- regression summary
- anomaly comparisons

---

## Scenario 5 — Root Cause Exploration

### Prompt

```text
What signals correlate most strongly with latency spikes?
```

### Drift Actions

- Fetch latency events
- Compare related signals
- Run correlation and lag analysis
- Rank possible drivers

### Response

Rendered outputs:
- correlation matrix
- scatter plots
- lag analysis
- ranked signal list

---

# Data Architecture

Critical design principle:

```text
Do not embed massive telemetry arrays directly into notebook state.
```

Instead use references:

```ts
{
  dataRef: "timeseries://gateway-17/cpu"
}
```

Benefits:
- caching
- virtualization
- streaming
- replay
- lazy loading
- efficient rendering

---

# Engine Adapter Interface

```ts
type EngineAdapter = {
  run(request: PromptRequest): Promise<PromptResponse>;
};
```

Example request:

```ts
type PromptRequest = {
  prompt: string;
  context?: {
    assetId?: string;
    timeRange?: {
      start: string;
      end: string;
    };
    investigationId?: string;
  };
};
```

Example response:

```ts
type PromptResponse = {
  blocks: RenderBlock[];
  metadata?: {
    engine: string;
    confidence?: number;
    dataSources?: string[];
  };
};
```

---

# Suggested MVP Stack

## Frontend

- React
- Plotly.js
- ECharts
- TipTap or Lexical for markdown editing if needed later

---

## Backend

- FastAPI or Node.js
- Langflow integration
- n8n webhooks
- PostgreSQL
- Redis

---

## Telemetry Sources

- Prometheus
- InfluxDB
- TimescaleDB
- OpenSearch
- MQTT
- Kafka

---

# MVP Scope

## Include

- Prompt input
- Rich response renderer
- Markdown rendering
- Plotly chart rendering
- Basic table rendering
- Engine adapter abstraction
- Langflow or custom backend adapter
- Simple investigation history
- Basic time-range and asset context

---

## Exclude Initially

- Full notebook programming model
- Multiple explicit cell types
- Collaborative editing
- Advanced dashboard builder
- Complex permissions
- Streaming charts
- Full MCP marketplace

---

# Long-Term Features

## Live updating investigations

Realtime telemetry updates inside response blocks.

---

## AI-generated dashboards

Example:

```text
Build a dashboard for thermal anomalies across all gateways.
```

---

## Collaborative investigations

Shared investigation sessions.

---

## Saved investigation templates

Examples:
- battery thermal analysis
- CPU regression workflow
- network instability investigation

---

## Agentic remediation

Future versions may allow Drift to:
- open tickets
- trigger n8n workflows
- run playbooks
- recommend setpoint changes
- simulate optimization changes
- create incident summaries

---

# Brand Notes

The name **Drift** works well because it naturally maps to:
- anomaly drift
- performance drift
- regression drift
- configuration drift
- optimization drift
- telemetry drift

It feels infrastructure-native and analytical without sounding overly AI-branded.

---

# Product Summary

Drift is an AI-enabled agentic observability platform.

Its first major UI component is a prompt-driven investigation notebook.

The notebook allows engineers to ask natural-language questions about assets, telemetry, anomalies, regressions, and optimization behavior.

Drift returns rich visual responses composed of markdown, dynamic charts, tables, timelines, and recommendations.
