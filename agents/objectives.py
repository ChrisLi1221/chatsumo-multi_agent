from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


WEIGHT_PROFILES: Dict[str, Dict[str, float]] = {
    "balanced": {
        "delay": 0.35,
        "co2": 0.20,
        "throughput": 0.25,
        "speed": 0.20,
    },
    "delay": {
        "delay": 0.50,
        "co2": 0.10,
        "throughput": 0.25,
        "speed": 0.15,
    },
    "throughput": {
        "delay": 0.30,
        "co2": 0.10,
        "throughput": 0.40,
        "speed": 0.20,
    },
    "emissions": {
        "delay": 0.25,
        "co2": 0.40,
        "throughput": 0.20,
        "speed": 0.15,
    },
}


METRIC_KEYS = {
    "delay": "mean_delay",
    "speed": "mean_speed",
    "throughput": "throughput",
    "co2": "co2",
}


LOWER_IS_BETTER = {"delay", "co2"}
HIGHER_IS_BETTER = {"speed", "throughput"}


def get_weight_profile(name: Optional[str] = None) -> Dict[str, float]:
    key = (name or "balanced").strip().lower()
    return dict(WEIGHT_PROFILES.get(key, WEIGHT_PROFILES["balanced"]))


def infer_weight_profile(user_text: str = "") -> str:
    text = (user_text or "").lower()
    if any(term in text for term in ["co2", "emission", "emissions", "pollution", "fuel"]):
        return "emissions"
    if any(term in text for term in ["throughput", "capacity", "vehicles served", "serve more"]):
        return "throughput"
    if any(term in text for term in ["delay", "waiting", "time loss", "congestion", "bottleneck"]):
        return "delay"
    return "balanced"


def _num(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _metric(kpi: Mapping[str, Any], metric: str) -> Optional[float]:
    key = METRIC_KEYS[metric]
    return _num(kpi.get(key))


def normalized_metric(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    metric: str,
) -> Optional[float]:
    cur = _metric(current, metric)
    base = _metric(baseline, metric)
    if cur is None or base is None or base == 0:
        return None
    return cur / base


def traffic_objective(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    weights: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    """
    Baseline-normalized traffic objective.

    Lower objective is better:
        J = wd * delay_norm + we * co2_norm
            - wq * throughput_norm - wv * speed_norm

    Missing metrics are skipped and the remaining weights are not renormalized,
    so the output remains transparent about which evidence was available.
    """
    w = dict(weights or WEIGHT_PROFILES["balanced"])
    terms: Dict[str, Dict[str, float]] = {}
    value = 0.0

    for metric in ["delay", "co2", "throughput", "speed"]:
        norm = normalized_metric(current, baseline, metric)
        if norm is None:
            continue
        weight = float(w.get(metric, 0.0))
        sign = 1.0 if metric in LOWER_IS_BETTER else -1.0
        contribution = sign * weight * norm
        terms[metric] = {
            "normalized": norm,
            "weight": weight,
            "contribution": contribution,
        }
        value += contribution

    return {
        "value": value,
        "lower_is_better": True,
        "weights": w,
        "terms": terms,
        "available_metrics": sorted(terms.keys()),
        "missing_metrics": sorted(set(METRIC_KEYS.keys()) - set(terms.keys())),
    }


def objective_delta(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    baseline: Optional[Mapping[str, Any]] = None,
    weights: Optional[Mapping[str, float]] = None,
) -> Dict[str, Any]:
    base = baseline or previous
    prev_obj = traffic_objective(previous, base, weights=weights)
    cur_obj = traffic_objective(current, base, weights=weights)
    delta = float(cur_obj["value"]) - float(prev_obj["value"])
    return {
        "previous": prev_obj,
        "current": cur_obj,
        "delta": delta,
        "improved": delta < 0,
    }
