# Prompt-Driven Observability Notebook — Simplified MVP Spec

## Vision

A prompt-native observability and investigation workspace for time-series systems.

Users type prompts in natural language.

The backend engine analyzes telemetry, runs workflows, performs anomaly detection or optimization, and returns rich interactive responses.

Responses are rendered as:
- markdown
- dynamic charts
- tables
- metrics
- timelines

The experience should feel like:

```text
ChatGPT + Grafana + Jupyter
```

without requiring users to write code.

---

# Core Product Idea

The user interacts through a single interface:

```text
Prompt → Rich Response
```

Example:

```text
User:
Why did gateway-17 become unstable yesterday?

System:
- markdown explanation
- anomaly charts
- timelines
- correlated metrics
- recommendations
```

No explicit:
- markdown cells
- query cells
- workflow cells
- notebook programming model

Those are implementation details hidden from the user.

---

# Product Goals

## 1. Prompt-native observability

Users should type:

```text
Why did CPU usage spike after deployment?
```

instead of:

```python
query(...)
plot(...)
```

---

## 2. Rich visual investigation

Responses are interactive and visual.

Supported outputs:
- markdown
- Plotly charts
- ECharts dashboards
- tables
- timelines
- metrics
- anomaly overlays

---

## 3. Time-series first

Designed primarily for:
- telemetry
- observability
- infrastructure
- edge devices
- industrial systems
- optimization systems
- IoT fleets
- energy systems

---

## 4. Engine agnostic

The frontend should work with:
- Langflow
- n8n
- MCP tools
- Python agents
- custom APIs
- SQL backends

without changing the UI.

---

# High-Level Architecture

```text
Frontend UI
    ↓
Notebook Runtime
    ↓
Engine Adapter Layer
    ↓
Execution Engines
    - Langflow
    - n8n
    - MCP tools
    - Custom AI backend
```

---

# User Experience

## Example Interaction

### User Prompt

```text
Why did gateway-17 become unstable yesterday afternoon?
```

### Response

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

# Response Rendering Model

Backend responses return structured render blocks.

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

# Why Plotly First

Primary renderer should be Plotly because the target domain is observability and time-series investigation.

Advantages:
- synchronized hover
- time zooming
- range sliders
- subplots
- annotations
- event overlays
- scatter analysis
- statistical plots
- notebook-style exploration

Ideal for:
- anomaly investigation
- telemetry analysis
- optimization visualization
- root cause analysis
- regression detection

---

# Why Support ECharts

ECharts is valuable for:
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

Adapters:
- LangflowAdapter
- N8NAdapter
- MCPAdapter
- PythonAdapter

---

# Suggested MVP Stack

## Frontend

- React
- TipTap or Lexical
- Plotly.js
- Zustand
- TanStack Query

---

## Backend

- HonoJS
- Cloudflare Workers, D1 and R2

- Langflow integration
- n8n webhooks

---

# Telemetry Sources

- Prometheus
- InfluxDB
- TimescaleDB
- OpenSearch
- MQTT
- Kafka

---

# Long-Term Features

## Live updating responses

Realtime telemetry updates.

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

# Product Positioning

Potential positioning:

```text
Prompt-native observability notebook
```

or

```text
AI-powered investigation workspace for time-series systems
```

or

```text
Jupyter for operational intelligence
```
