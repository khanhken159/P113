"""CLI của Optimizer Agent. Dùng trực tiếp trên Windows (không có make) hoặc qua Makefile.

    python -m src.optimize_agent.cli setup    [--dataset tpch|eltbench|all]
    python -m src.optimize_agent.cli baseline [--dataset ...]
    python -m src.optimize_agent.cli optimize [--dataset ...] [--k 3] [--only q01] [--no-llm]
    python -m src.optimize_agent.cli eval     [--runs RUN_ID ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import duckdb

from src.optimize_agent.agent import AgentOptions, BaselineCache, OptimizerAgent
from src.optimize_agent.datasets.base import ModelSpec
from src.optimize_agent.datasets.tpch import load_tpch_models, setup_tpch
from src.optimize_agent.metrics import compute_metrics, pass_at_k, rejection_rate
from src.optimize_agent.report import metrics_table, write_reports
from src.optimize_agent.sandbox import connect, environment_info
from src.optimize_agent.settings import AgentConfig, load_config

DATASETS = ("tpch", "eltbench")


def _datasets(choice: str, cfg: AgentConfig) -> list[str]:
    chosen = list(DATASETS) if choice == "all" else [choice]
    if "eltbench" in chosen and not cfg.eltbench.databases:
        print("[eltbench] chưa cấu hình eltbench.databases trong config.yaml -> bỏ qua")
        chosen.remove("eltbench")
    return chosen


def load_models(con: duckdb.DuckDBPyConnection, cfg: AgentConfig, dataset: str) -> list[ModelSpec]:
    if dataset == "tpch":
        con.execute("LOAD tpch")
        return load_tpch_models(con, cfg)
    from src.optimize_agent.datasets.eltbench import load_eltbench_models

    return load_eltbench_models(con, cfg)


def cmd_setup(args: argparse.Namespace, cfg: AgentConfig) -> None:
    con = connect(cfg)
    for dataset in _datasets(args.dataset, cfg):
        if dataset == "tpch":
            print(setup_tpch(con, cfg))
        else:
            from src.optimize_agent.datasets.eltbench import setup_eltbench

            print(setup_eltbench(con, cfg))


def _run_dataset(con: duckdb.DuckDBPyConnection, cfg: AgentConfig, run_dir: Path, dataset: str, args: argparse.Namespace, options: AgentOptions) -> list[dict[str, Any]]:  # noqa: PLR0913
    cache = BaselineCache(cfg.resolve(cfg.reports_dir) / "baseline", dataset)
    agent = OptimizerAgent(con, cfg, run_dir, cache)
    results = []
    for model in load_models(con, cfg, dataset):
        if args.only and args.only not in model.name:
            continue
        start = time.perf_counter()
        result = agent.run_model(model, options)
        proposal = result.get("proposal")
        status = f"speedup {proposal['speedup']}x" if proposal else ("chậm, không có đề xuất" if result["detect"]["is_slow"] else "không chậm")
        print(f"  {model.name:<45} base {result['baseline']['timing']['median_s']:7.3f}s  {status}  ({time.perf_counter() - start:.0f}s)", flush=True)
        results.append(result)
    return results


def _write_run(run_dir: Path, run_id: str, env: dict[str, Any], cfg: AgentConfig, by_dataset: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    metrics = {d: compute_metrics(models, cfg.optimize.min_speedup) for d, models in by_dataset.items()}
    payload = {
        "run_id": run_id,
        "environment": env,
        "config": cfg.model_dump(),
        "metrics": metrics,
        "models": [m for models in by_dataset.values() for m in models],
    }
    write_reports(run_dir, payload)
    return metrics


def cmd_run(args: argparse.Namespace, cfg: AgentConfig, baseline_only: bool) -> None:
    con = connect(cfg)
    env = environment_info(con)
    reports = cfg.resolve(cfg.reports_dir)
    base_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    prefix = "baseline-" if baseline_only else ""
    k = 1 if baseline_only else args.k
    run_ids = [f"{prefix}{base_id}" + (f"-k{i}" if k > 1 else "") for i in range(1, k + 1)]
    options = AgentOptions(use_llm=not args.no_llm, baseline_only=baseline_only)
    datasets = _datasets(args.dataset, cfg)
    for run_id in run_ids:
        print(f"== run {run_id}")
        run_dir = reports / run_id
        by_dataset = {d: _run_dataset(con, cfg, run_dir, d, args, options) for d in datasets}
        metrics = _write_run(run_dir, run_id, env, cfg, by_dataset)
        print(metrics_table(metrics))
        print(f"-> {run_dir / 'report.md'}")
    if not baseline_only:
        (reports / "latest.json").write_text(json.dumps({"runs": run_ids, "datasets": datasets}, indent=2), encoding="utf-8")


def _load_run(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "report.json").read_text(encoding="utf-8"))


def cmd_eval(args: argparse.Namespace, cfg: AgentConfig) -> None:
    """Tính lại chỉ số mục 5.2 từ các run (mặc định: lần optimize gần nhất), gồm pass@k và rejection_rate."""
    reports = cfg.resolve(cfg.reports_dir)
    if not args.runs and not (reports / "latest.json").exists():
        print("Chưa có run nào -> chạy optimize trước (máy sạch)")
        run_args = argparse.Namespace(dataset="all", run_id=None, only=None, no_llm=False, k=1)
        cmd_run(run_args, cfg, baseline_only=False)
    run_ids = args.runs or json.loads((reports / "latest.json").read_text(encoding="utf-8"))["runs"]
    runs = [_load_run(reports / r) for r in run_ids]
    first = runs[0]
    review_path = reports / run_ids[0] / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    metrics: dict[str, dict[str, Any]] = {}
    for dataset in sorted({m["dataset"] for m in first["models"]}):
        per_run = [[m for m in run["models"] if m["dataset"] == dataset] for run in runs]
        metrics[dataset] = compute_metrics(per_run[0], cfg.optimize.min_speedup)
        metrics[dataset].update(pass_at_k(per_run, cfg.optimize.min_speedup))
        names = {m["name"] for m in per_run[0]}
        metrics[dataset]["rejection_rate"] = rejection_rate({"proposals": {n: d for n, d in review.get("proposals", {}).items() if n in names}})
    first["metrics"], first["eval_runs"] = metrics, run_ids
    write_reports(reports / run_ids[0], first)
    summary = f"# Tổng hợp chỉ số — runs {', '.join(run_ids)}\n\n{metrics_table(metrics)}\n"
    (reports / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="optimize_agent", description="Optimizer Agent (Phase 1)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "baseline", "optimize"):
        p = sub.add_parser(name)
        p.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
        if name != "setup":
            p.add_argument("--run-id", default=None)
            p.add_argument("--only", default=None, help="chỉ chạy model có tên chứa chuỗi này")
            p.add_argument("--no-llm", action="store_true")
            p.add_argument("--k", type=int, default=1, help="số lần chạy agent (pass@k)")
    ev = sub.add_parser("eval")
    ev.add_argument("--runs", nargs="*", default=None)
    parser.add_argument("--config", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "setup":
        cmd_setup(args, cfg)
    elif args.command == "baseline":
        cmd_run(args, cfg, baseline_only=True)
    elif args.command == "optimize":
        cmd_run(args, cfg, baseline_only=False)
    else:
        cmd_eval(args, cfg)


if __name__ == "__main__":
    main()
