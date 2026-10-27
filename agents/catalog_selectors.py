"""Matched non-LLM selectors over the finite mutation catalog.

These selectors exist to isolate the contribution of *LLM mutation selection*
from the surrounding beam-search structure.  The paper's ``grounded_random``
baseline differs from the LLM condition in how the whole remaining simulation
budget is allocated, so it cannot answer the narrower question: given the same
retained parent and the same deterministically enumerated catalog of legal
mutations, does an LLM choose better than a cheap selector?

Each selector here is a drop-in replacement for
:func:`agents.llm_agents.call_llm` as used by
:func:`agents.debate_optimizer.run_feedback_refinement`.  That function builds
the catalog deterministically *before* any model call and passes it inside the
``user`` payload, so substituting the callable holds everything else fixed:
same grounded initialization seed, same parent, same catalog, same beam width,
same per-case SUMO budget, same final selector.  Only the pick differs.

Two conditions are provided:

``catalog_first``
    Take the enumerator's own highest-priority entry.  This is the
    "rule-based selector" baseline.  It is a meaningful control because
    :func:`agents.debate_optimizer.build_finite_mutation_catalog` already
    orders candidates heuristically (for example, preferring conservative
    parameter values when the parent violated a safety constraint), and the
    specialist prompts explicitly instruct the model *not* to default to the
    first entry.  Comparing against it therefore tests whether the LLM adds
    anything over the program's own ordering.

``catalog_random``
    Draw uniformly from the catalog using a per-case seed.  This is the
    "identical-parent random selector" baseline.

Reporting note: these selectors never emit malformed output, whereas the LLM
condition loses part of its budget to empty or length-limited JSON.  That
asymmetry is real and must be reported rather than hidden, so every response
records ``selector_kind``, the catalog size, and the chosen index.
"""

from __future__ import annotations

import json
import random
from typing import Any, Callable, Dict, List, Mapping, Optional

Json = Dict[str, Any]

CATALOG_FIRST = "catalog_first"
CATALOG_RANDOM = "catalog_random"
CATALOG_SELECTOR_KINDS = (CATALOG_FIRST, CATALOG_RANDOM)


def _extract_catalog(user: str) -> List[Mapping[str, Any]]:
    """Recover the mutation catalog from the refinement request payload."""
    try:
        payload = json.loads(user or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, Mapping):
        return []
    catalog = payload.get("mutation_catalog")
    if not isinstance(catalog, list):
        return []
    return [row for row in catalog if isinstance(row, Mapping) and row.get("mutation_id")]


def _empty_response(selector_kind: str) -> Json:
    return {
        "selected_mutation_id": "",
        "claim": "No finite mutation was available for selection.",
        "reason": f"{selector_kind}: mutation_catalog was empty.",
        "evidence_refs": ["simulation_feedback"],
        "risk": "No unseen validator-confirmed mutation is available.",
        "selector_kind": selector_kind,
        "mutation_catalog_size": 0,
        "selected_index": None,
    }


def _response(
    selector_kind: str,
    catalog: List[Mapping[str, Any]],
    index: int,
    reason: str,
) -> Json:
    chosen = catalog[index]
    return {
        "selected_mutation_id": str(chosen.get("mutation_id")),
        "claim": str(chosen.get("hint") or ""),
        "reason": reason,
        "evidence_refs": ["simulation_feedback"],
        "risk": (
            "The validator-confirmed mutation may still transfer congestion "
            "downstream; this selector does not reason about that risk."
        ),
        "selector_kind": selector_kind,
        "mutation_catalog_size": len(catalog),
        "selected_index": int(index),
    }


def make_catalog_first_selector() -> Callable[..., Json]:
    """Rule-based control: always take the enumerator's first (highest-priority) entry."""

    def selector(
        system: str,
        user: str,
        *,
        model: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        retries: int = 2,
        force_json: bool = True,
        state: Optional[Any] = None,
        usage_scope: str = "llm",
    ) -> Json:
        catalog = _extract_catalog(user)
        if not catalog:
            return _empty_response(CATALOG_FIRST)
        return _response(
            CATALOG_FIRST,
            catalog,
            0,
            "Deterministic rule: take the catalog's highest-priority entry.",
        )

    return selector


def make_catalog_random_selector(seed: Optional[int] = None) -> Callable[..., Json]:
    """Random control: draw uniformly from the same catalog under a fixed seed."""
    rng = random.Random(seed)

    def selector(
        system: str,
        user: str,
        *,
        model: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        max_tokens: int = 1024,
        timeout: float = 60.0,
        retries: int = 2,
        force_json: bool = True,
        state: Optional[Any] = None,
        usage_scope: str = "llm",
    ) -> Json:
        catalog = _extract_catalog(user)
        if not catalog:
            return _empty_response(CATALOG_RANDOM)
        index = rng.randrange(len(catalog))
        return _response(
            CATALOG_RANDOM,
            catalog,
            index,
            f"Uniform random draw from {len(catalog)} legal mutations.",
        )

    return selector


def make_catalog_selector(kind: str, seed: Optional[int] = None) -> Callable[..., Json]:
    """Build a matched catalog selector by name."""
    if kind == CATALOG_FIRST:
        return make_catalog_first_selector()
    if kind == CATALOG_RANDOM:
        return make_catalog_random_selector(seed)
    raise ValueError(
        f"Unknown catalog selector '{kind}'; expected one of {CATALOG_SELECTOR_KINDS}"
    )
