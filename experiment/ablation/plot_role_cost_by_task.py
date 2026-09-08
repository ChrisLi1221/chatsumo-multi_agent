import argparse
import json
import os

# Make plotting robust in headless / restricted environments.
os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="experiment/role_costs.json", help="Path to role_costs.json")
    ap.add_argument("--output", default="experiment/role_costs_by_task.png", help="Output PNG path")
    args = ap.parse_args()

    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except Exception as e:
        raise SystemExit(f"matplotlib/numpy not available: {e}")

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    overall = (data or {}).get("overall") or {}
    scenarios = overall.get("scenarios") or []
    roles = overall.get("role_order") or []
    matrix = overall.get("scenario_role_matrix") or {}

    if not scenarios or not roles or not matrix:
        raise SystemExit("Missing scenario-role matrix in input JSON. Re-run analyze_role_costs.py first.")

    x = np.arange(len(roles))
    width = 0.22
    offsets = []
    if len(scenarios) == 1:
        offsets = [0.0]
    elif len(scenarios) == 2:
        offsets = [-width / 2, width / 2]
    else:
        offsets = [-(len(scenarios) - 1) * width / 2 + i * width for i in range(len(scenarios))]

    plt.figure(figsize=(11, 6))

    for i, sc in enumerate(scenarios):
        vals = [int((matrix.get(sc) or {}).get(role, 0)) for role in roles]
        plt.bar(x + offsets[i], vals, width=width, label=sc)

    plt.xticks(x, roles, rotation=20)
    plt.ylabel("Total Tokens")
    plt.title("Role Token Cost by Task Complexity")
    plt.legend(title="Task")
    plt.tight_layout()

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    plt.savefig(args.output, dpi=220)
    print(f"[*] Saved grouped bar chart: {args.output}")


if __name__ == "__main__":
    main()
