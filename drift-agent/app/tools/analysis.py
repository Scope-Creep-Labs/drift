from __future__ import annotations

import numpy as np
from scipy import stats

from .metrics import ToolContext


def _trace_values(trace: dict) -> np.ndarray:
    return np.array([v for v in trace.get("y", []) if v is not None and np.isfinite(v)])


def _all_traces(ctx: ToolContext, ref: str) -> list[dict] | None:
    return ctx.data_cache.get(ref)


async def summarize_series(ctx: ToolContext, args: dict) -> dict:
    ref = args["ref"]
    traces = _all_traces(ctx, ref)
    if not traces:
        return {"error": f"unknown ref: {ref}"}
    out: list[dict] = []
    for t in traces:
        vals = _trace_values(t)
        if vals.size == 0:
            out.append({"name": t.get("name"), "n": 0})
            continue
        slope = float(np.polyfit(np.arange(vals.size), vals, 1)[0]) if vals.size >= 2 else 0.0
        out.append(
            {
                "name": t.get("name"),
                "n": int(vals.size),
                "mean": float(vals.mean()),
                "stddev": float(vals.std()),
                "min": float(vals.min()),
                "max": float(vals.max()),
                "p50": float(np.percentile(vals, 50)),
                "p90": float(np.percentile(vals, 90)),
                "p95": float(np.percentile(vals, 95)),
                "p99": float(np.percentile(vals, 99)),
                "slope_per_step": slope,
            }
        )
    return {"ref": ref, "series": out}


async def detect_anomalies(ctx: ToolContext, args: dict) -> dict:
    ref = args["ref"]
    method = args.get("method", "zscore")
    threshold = float(args.get("threshold", 3.0))
    traces = _all_traces(ctx, ref)
    if not traces:
        return {"error": f"unknown ref: {ref}"}
    findings: list[dict] = []
    for t in traces:
        vals = _trace_values(t)
        if vals.size < 5:
            continue
        if method == "iqr":
            q1, q3 = np.percentile(vals, [25, 75])
            iqr = q3 - q1
            lo, hi = q1 - threshold * iqr, q3 + threshold * iqr
            mask = (vals < lo) | (vals > hi)
        else:  # zscore
            mu, sigma = vals.mean(), vals.std() or 1.0
            mask = np.abs(vals - mu) > threshold * sigma
        idx = np.where(mask)[0].tolist()
        x = t.get("x") or []
        anomalies = [
            {"index": int(i), "ts": x[i] if i < len(x) else None, "value": float(vals[i])}
            for i in idx[:25]  # cap for token budget
        ]
        findings.append(
            {
                "name": t.get("name"),
                "method": method,
                "threshold": threshold,
                "n_anomalies": int(mask.sum()),
                "first_25": anomalies,
            }
        )
    return {"ref": ref, "findings": findings}


async def correlate(ctx: ToolContext, args: dict) -> dict:
    ref_a = args["ref_a"]
    ref_b = args["ref_b"]
    a_traces = _all_traces(ctx, ref_a)
    b_traces = _all_traces(ctx, ref_b)
    if not a_traces or not b_traces:
        return {"error": "one or both refs unknown"}
    pairs: list[dict] = []
    for ta in a_traces:
        for tb in b_traces:
            va = _trace_values(ta)
            vb = _trace_values(tb)
            n = min(va.size, vb.size)
            if n < 5:
                continue
            va, vb = va[:n], vb[:n]
            r, p = stats.pearsonr(va, vb)
            # crude lag scan in [-10, +10]
            best_lag, best_r = 0, abs(float(r))
            for lag in range(-10, 11):
                if lag == 0:
                    continue
                if lag > 0:
                    aa, bb = va[:-lag], vb[lag:]
                else:
                    aa, bb = va[-lag:], vb[:lag]
                if aa.size < 5:
                    continue
                rr = stats.pearsonr(aa, bb)[0]
                if abs(float(rr)) > best_r:
                    best_r, best_lag = abs(float(rr)), lag
            pairs.append(
                {
                    "a": ta.get("name"),
                    "b": tb.get("name"),
                    "pearson_r": float(r),
                    "p_value": float(p),
                    "best_abs_r_with_lag": best_r,
                    "best_lag_steps": best_lag,
                }
            )
    pairs.sort(key=lambda p: -abs(p["pearson_r"]))
    return {"pairs": pairs[:25]}


async def compare_distributions(ctx: ToolContext, args: dict) -> dict:
    ref_a = args["ref_a"]
    ref_b = args["ref_b"]
    a_traces = _all_traces(ctx, ref_a)
    b_traces = _all_traces(ctx, ref_b)
    if not a_traces or not b_traces:
        return {"error": "one or both refs unknown"}
    a = _trace_values(a_traces[0])
    b = _trace_values(b_traces[0])
    if a.size < 5 or b.size < 5:
        return {"error": "need at least 5 points per ref"}
    ks_stat, ks_p = stats.ks_2samp(a, b)
    return {
        "a": a_traces[0].get("name"),
        "b": b_traces[0].get("name"),
        "n_a": int(a.size),
        "n_b": int(b.size),
        "ks_statistic": float(ks_stat),
        "ks_p_value": float(ks_p),
        "mean_delta": float(b.mean() - a.mean()),
        "p50_delta": float(np.percentile(b, 50) - np.percentile(a, 50)),
        "p95_delta": float(np.percentile(b, 95) - np.percentile(a, 95)),
        "p99_delta": float(np.percentile(b, 99) - np.percentile(a, 99)),
    }


async def detect_change_point(ctx: ToolContext, args: dict) -> dict:
    """Simple cumulative-sum change-point: index where cumulative deviation peaks."""
    ref = args["ref"]
    traces = _all_traces(ctx, ref)
    if not traces:
        return {"error": f"unknown ref: {ref}"}
    out: list[dict] = []
    for t in traces:
        vals = _trace_values(t)
        if vals.size < 10:
            continue
        mu = vals.mean()
        cusum = np.cumsum(vals - mu)
        idx = int(np.argmax(np.abs(cusum)))
        x = t.get("x") or []
        out.append(
            {
                "name": t.get("name"),
                "index": idx,
                "ts": x[idx] if idx < len(x) else None,
                "before_mean": float(vals[:idx].mean()) if idx > 0 else None,
                "after_mean": float(vals[idx:].mean()) if idx < vals.size else None,
                "score": float(abs(cusum[idx])),
            }
        )
    return {"ref": ref, "change_points": out}


ANALYSIS_TOOLS: list[dict] = [
    {
        "name": "summarize_series",
        "description": "Compute basic statistics for each series in a ref: n, mean, stddev, min, max, p50/p90/p95/p99, slope.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
    {
        "name": "detect_anomalies",
        "description": (
            "Find anomalous points in each series of a ref using z-score (default) or IQR. "
            "Returns up to first 25 anomaly indices per series."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "method": {"type": "string", "enum": ["zscore", "iqr"]},
                "threshold": {"type": "number", "description": "Z-score threshold (default 3.0) or IQR multiplier (default 3.0)."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "correlate",
        "description": (
            "Compute Pearson correlation between every pair of series across two refs, "
            "with a small lag scan to find the strongest relationship within ±10 steps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref_a": {"type": "string"},
                "ref_b": {"type": "string"},
            },
            "required": ["ref_a", "ref_b"],
        },
    },
    {
        "name": "compare_distributions",
        "description": "Two-sample KS test + percentile deltas (p50/p95/p99) on the first series of each ref. Use to detect regressions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref_a": {"type": "string"},
                "ref_b": {"type": "string"},
            },
            "required": ["ref_a", "ref_b"],
        },
    },
    {
        "name": "detect_change_point",
        "description": "CUSUM-based change-point detection. Returns the most likely change index and before/after means for each series.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
]


ANALYSIS_HANDLERS = {
    "summarize_series": summarize_series,
    "detect_anomalies": detect_anomalies,
    "correlate": correlate,
    "compare_distributions": compare_distributions,
    "detect_change_point": detect_change_point,
}
