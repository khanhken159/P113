"""Local bridge from the demo UI to an isolated FlowForge agent run."""

from __future__ import annotations

import csv
import io
import re
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "presentation" / "ui"
UI_PROVIDER = os.environ.get("FLOWFORGE_UI_PROVIDER", "openai").strip().lower()
if UI_PROVIDER not in {"openai", "gemini", "mock"}:
    raise ValueError("FLOWFORGE_UI_PROVIDER must be openai, gemini, or mock")
RUN_LOCK = threading.Lock()
PENDING_RUNS: dict[str, dict] = {}
MAX_REQUEST_BYTES = 32 * 1024 * 1024
FILE_NAMES = {
    "shopee": "orders_shopee.csv",
    "tiki": "orders_tiki.csv",
    "website": "orders_website.json",
}


def create_run_workspace(uploads: list[dict], selected_sources: list[str]) -> tuple[tempfile.TemporaryDirectory, Path, list[str]]:
    temp = tempfile.TemporaryDirectory(prefix="flowforge-run-")
    run_root = Path(temp.name)
    (run_root / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "run_pipeline.py", run_root / "scripts" / "run_pipeline.py")
    shutil.copy2(ROOT / "scripts" / "retest.py", run_root / "scripts" / "retest.py")
    shutil.copy2(ROOT / "scripts" / "decide_review.py", run_root / "scripts" / "decide_review.py")
    shutil.copytree(ROOT / "src", run_root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "agents", run_root / "agents", ignore=shutil.ignore_patterns("__pycache__"))
    data_dir = run_root / "data"
    shutil.copytree(ROOT / "eval" / "results" / "flowforge" / "data", data_dir)
    generated_dir = run_root / "generated"
    output_dir = run_root / "output"
    generated_dir.mkdir()
    output_dir.mkdir()

    available_sources = [source for source in FILE_NAMES if source in selected_sources] if selected_sources else list(FILE_NAMES)
    if uploads:
        for existing in data_dir.iterdir():
            if existing.is_file():
                existing.unlink()
        uploaded_sources = []
        uploaded_standard_csv = []
        custom_dir = data_dir / "custom_csv"
        custom_files = []
        for index, upload in enumerate(uploads, start=1):
            raw_name = Path(str(upload.get("name", "dataset.csv"))).name
            original_name = raw_name.lower()
            content = upload.get("content")
            if not isinstance(content, str) or not content.strip():
                temp.cleanup()
                raise ValueError(f"File {raw_name} is empty or unreadable.")
            # CSVs always use schema-based planning; their filenames do not select a source.
            source = next((key for key in FILE_NAMES if key in original_name), None) if Path(raw_name).suffix.lower() != ".csv" else None
            if source is not None:
                expected_headers = {
                    "shopee": {"order_id", "order_date", "total_amount", "status"},
                    "tiki": {"id", "created_at", "amount", "order_status"},
                    "website": {"OrderID", "OrderDate", "GrandTotal", "Status"},
                }.get(source)
                if Path(raw_name).suffix.lower() == ".csv" and expected_headers:
                    actual_headers = {column.strip() for column in next(csv.reader(io.StringIO(content.lstrip("\ufeff"))), [])}
                    if not expected_headers.issubset(actual_headers):
                        source = None
            if source is not None:
                destination = data_dir / FILE_NAMES[source]
                destination.write_text(content, encoding="utf-8-sig")
                if source not in uploaded_sources:
                    uploaded_sources.append(source)
                if Path(raw_name).suffix.lower() == ".csv":
                    uploaded_standard_csv.append((source, raw_name, content))
                continue
            if Path(raw_name).suffix.lower() != ".csv":
                temp.cleanup()
                raise ValueError("Custom datasets must be CSV files; the filename may be arbitrary.")
            try:
                columns = next(csv.reader(io.StringIO(content.lstrip("\ufeff"))), [])
            except csv.Error as error:
                temp.cleanup()
                raise ValueError(f"Could not read CSV header in {raw_name}: {error}") from error
            columns = [column.strip() for column in columns]
            if not columns or not any(columns):
                temp.cleanup()
                raise ValueError(f"CSV file {raw_name} has no header row.")
            custom_dir.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(raw_name).stem).strip("_") or f"dataset_{index}"
            safe_name = f"{index}_{stem}.csv"
            (custom_dir / safe_name).write_text(content, encoding="utf-8-sig")
            custom_files.append({"name": safe_name, "original_name": raw_name, "columns": columns})
        if custom_files and uploaded_sources:
            if "website" in uploaded_sources:
                temp.cleanup()
                raise ValueError("Combine custom CSV files with other CSV files only; Website JSON uses its own input mode.")
            for source in uploaded_sources:
                (data_dir / FILE_NAMES[source]).unlink(missing_ok=True)
            for index, (source, raw_name, content) in enumerate(uploaded_standard_csv, start=len(custom_files) + 1):
                columns = [column.strip() for column in next(csv.reader(io.StringIO(content.lstrip("\ufeff"))), [])]
                custom_dir.mkdir(parents=True, exist_ok=True)
                stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(raw_name).stem).strip("_") or f"dataset_{index}"
                safe_name = f"{index}_{stem}.csv"
                (custom_dir / safe_name).write_text(content, encoding="utf-8-sig")
                custom_files.append({"name": safe_name, "original_name": raw_name, "columns": columns})
            uploaded_sources = []
        available_sources = [source for source in uploaded_sources if source in available_sources]
        if custom_files:
            (data_dir / "custom_csv_schema.json").write_text(json.dumps(custom_files, ensure_ascii=False, indent=2), encoding="utf-8")
            available_sources.append("custom_csv")
    for source, file_name in FILE_NAMES.items():
        if source not in available_sources:
            (data_dir / file_name).unlink(missing_ok=True)
    if not available_sources:
        temp.cleanup()
        raise ValueError("Upload at least one selected source file or a custom CSV dataset.")

    return temp, run_root, available_sources


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        request_path = urlparse(self.path).path
        if request_path == "/api/status":
            return self.send_json(200, {"ready": (ROOT / "scripts" / "run_pipeline.py").is_file(), "provider": UI_PROVIDER})
        if ".env" in Path(request_path).parts:
            return self.send_json(404, {"error": "Not found"})
        return super().do_GET()

    def do_POST(self):
        request_path = urlparse(self.path).path
        if request_path == "/api/retest":
            return self.handle_retest()
        if request_path == "/api/review":
            return self.handle_review()
        if request_path != "/api/run":
            return self.send_json(404, {"error": "Not found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > MAX_REQUEST_BYTES:
                return self.send_json(413, {"error": "Tổng dung lượng yêu cầu vượt quá 32 MB."})
            body = json.loads(self.rfile.read(size) or b"{}")
            request = str(body.get("request", "")).strip()
            uploads = body.get("uploads", [])
            selected_sources = body.get("selected_sources", [])
            clarification = body.get("clarification", {})
            if not request:
                return self.send_json(400, {"error": "Requirement is empty"})
            if not isinstance(uploads, list) or len(uploads) > 10 or not isinstance(selected_sources, list) or not isinstance(clarification, dict):
                return self.send_json(400, {"error": "Chỉ nhận tối đa 10 file CSV/JSON."})

            with RUN_LOCK:
                temp, run_root, sources = create_run_workspace(uploads, selected_sources)
                try:
                    env = self.run_environment(sources, run_root)
                    run = subprocess.run(
                        [sys.executable, "scripts/run_pipeline.py", "--request", request, "--provider", UI_PROVIDER, "--session", "ui-run", "--reset-session", "--defer-human-review", "--clarification", json.dumps(clarification, ensure_ascii=False)],
                        cwd=run_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=env,
                    )
                    if run.returncode:
                        return self.send_json(500, {"error": (run.stderr or run.stdout)[-5000:]})
                    result = json.loads(run.stdout)
                    if result.get("status") == "clarification_required":
                        return self.send_json(200, result)
                    if result.get("status") == "failed":
                        return self.send_json(422, {"error": result.get("error", "Agent workflow failed"), "workflow": result})
                    result = self.attach_run_data(result, run_root, sources)
                    if result.get("test_status") != "PASS":
                        run_id = uuid.uuid4().hex
                        report = result.get("test_report", {})
                        (run_root / "generated" / "ui_state.json").write_text(json.dumps({
                            "request": request, "provider": UI_PROVIDER, "approved": False,
                            "interactive_review": False, "initial_metrics": report.get("metrics", {}),
                            "human_review_attempts": 0, "artifacts": result.get("artifacts", {}),
                        }, ensure_ascii=False, indent=2), encoding="utf-8")
                        PENDING_RUNS[run_id] = {"temp": temp, "run_root": run_root, "sources": sources}
                        result["run_id"] = run_id
                        result["human_review_status"] = "PENDING"
                        return self.send_json(200, result)
                    if result.get("review_status") != "APPROVED":
                        run_id = uuid.uuid4().hex
                        PENDING_RUNS[run_id] = {
                            "temp": temp, "run_root": run_root, "sources": sources,
                            "result": result,
                        }
                        result["run_id"] = run_id
                        result["human_review_status"] = "PENDING"
                        return self.send_json(200, result)
                    return self.send_json(200, result)
                finally:
                    if not any(item.get("temp") is temp for item in PENDING_RUNS.values()):
                        temp.cleanup()
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "Agent pipeline timed out"})
        except Exception as error:
            return self.send_json(500, {"error": f"{type(error).__name__}: {error}"})

    def run_environment(self, sources, run_root=None):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "FLOWFORGE_AVAILABLE_SOURCES": ",".join(sources)}
        env["FLOWFORGE_DATA_DIR"] = str(run_root / "data") if run_root else str(ROOT / "eval" / "results" / "flowforge" / "data")
        if UI_PROVIDER == "openai":
            env["FLOWFORGE_AVAILABLE_SOURCES"] = ",".join(sources)
        # Keep provider credentials in the child process environment only.
        for env_file in (ROOT / ".env",):
            if env_file.is_file():
                for key, value in dotenv_values(env_file).items():
                    if value is not None and key not in env:
                        env[key] = value
        env["LANGCHAIN_TRACING_V2"] = "false"
        env["LANGSMITH_TRACING"] = "false"
        schema_path = run_root / "data" / "custom_csv_schema.json" if run_root else None
        if schema_path and schema_path.exists():
            schemas = json.loads(schema_path.read_text(encoding="utf-8"))
            env["FLOWFORGE_CUSTOM_CSV_SCHEMAS"] = json.dumps(schemas, ensure_ascii=False)
        return env

    def attach_run_data(self, result, run_root, sources):
        generated = run_root / "generated"
        read_json = lambda name: json.loads((generated / name).read_text(encoding="utf-8")) if (generated / name).exists() else None
        optimizer_runs = read_json("sql_optimization_report.json") or []
        optimizer_details = []
        for item in optimizer_runs:
            report_path = Path(item.get("report", ""))
            if report_path.is_file():
                optimizer_details.append(json.loads(report_path.read_text(encoding="utf-8")))
        manifest = read_json("output_manifest.json") or {}
        output_files = []
        for output in manifest.get("outputs", []):
            output_path = run_root / "output" / str(output.get("file_name", ""))
            if not output_path.is_file():
                continue
            file_format = str(output.get("format", output_path.suffix.lstrip("."))).lower()
            if file_format == "json":
                try:
                    rows_for_output = json.loads(output_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    rows_for_output = []
            else:
                with output_path.open(encoding="utf-8-sig", newline="") as stream:
                    rows_for_output = list(csv.DictReader(stream))
            output_files.append({
                "file_name": output.get("file_name", output_path.name),
                "format": file_format,
                "columns": output.get("columns", []),
                "rows": rows_for_output,
                "row_count": output.get("row_count", len(rows_for_output)),
            })
        output_path = run_root / "output" / "fct_daily_revenue.csv"
        rows = output_files[0]["rows"] if output_files else []
        if not output_files and output_path.exists():
            with output_path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        result.update({
            "plan": read_json("pipeline_plan.json"),
            "test_report": read_json("test_report.json"),
            "review_decision": read_json("review_decision.json"),
            "comparison": read_json("before_after.json"),
            "output_rows": rows,
            "output_files": output_files,
            "output_manifest": manifest,
            "pipeline_code": (generated / "generated_pipeline.py").read_text(encoding="utf-8") if (generated / "generated_pipeline.py").exists() else "",
            "sql_baseline": (generated / "sql_baseline.sql").read_text(encoding="utf-8") if (generated / "sql_baseline.sql").exists() else "",
            "sql_query": (generated / "generated_query.sql").read_text(encoding="utf-8") if (generated / "generated_query.sql").exists() else "",
            "sql_optimization_report": optimizer_runs,
            "sql_optimizer_details": optimizer_details,
            "used_sources": sources,
        })
        return result

    def handle_retest(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > MAX_REQUEST_BYTES:
                return self.send_json(413, {"error": "Request too large"})
            body = json.loads(self.rfile.read(size) or b"{}")
            run_id = str(body.get("run_id", ""))
            code = body.get("pipeline_code")
            pending = PENDING_RUNS.get(run_id)
            if not pending:
                return self.send_json(404, {"error": "Pending run not found; start a new pipeline run."})
            if not isinstance(code, str) or len(code.encode("utf-8")) > MAX_REQUEST_BYTES:
                return self.send_json(400, {"error": "Pipeline code is missing or too large."})
            run_root = pending["run_root"]
            pipeline = run_root / "generated" / "generated_pipeline.py"
            pipeline.write_text(code, encoding="utf-8")
            retest = subprocess.run(
                [sys.executable, "scripts/retest.py"], cwd=run_root,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90, env=self.run_environment(pending["sources"], run_root),
            )
            if retest.returncode:
                return self.send_json(500, {"error": (retest.stderr or retest.stdout)[-5000:]})
            workflow = json.loads(retest.stdout)
            result = self.attach_run_data(workflow, run_root, pending["sources"])
            if result.get("test_status") == "PASS" and result.get("review_status") == "APPROVED":
                result["run_id"] = None
                PENDING_RUNS.pop(run_id, None)
                pending["temp"].cleanup()
            elif result.get("test_status") == "PASS":
                result["run_id"] = run_id
                result["human_review_status"] = "PENDING"
                pending["result"] = result
            else:
                result["run_id"] = run_id
                result["human_review_status"] = "PENDING"
                pending["result"] = result
            return self.send_json(200, result)
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "Retest timed out"})
        except Exception as error:
            return self.send_json(500, {"error": f"{type(error).__name__}: {error}"})

    def handle_review(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 64 * 1024:
                return self.send_json(413, {"error": "Review request too large"})
            body = json.loads(self.rfile.read(size) or b"{}")
            run_id = str(body.get("run_id", ""))
            decision = str(body.get("decision", "")).lower()
            reason = str(body.get("reason", "")).strip()[:2000]
            pending = PENDING_RUNS.get(run_id)
            if not pending:
                return self.send_json(404, {"error": "Pending review not found; start a new pipeline run."})
            current = pending.get("result", {})
            if current.get("test_status") != "PASS" or current.get("acceptance_status") != "ACCEPTED":
                return self.send_json(409, {"error": "Only a passing, accepted pipeline can enter Reviewer approval."})
            if decision not in {"approve", "reject"}:
                return self.send_json(400, {"error": "Decision must be approve or reject."})
            if decision == "reject" and not reason:
                return self.send_json(400, {"error": "Add a reason before rejecting the pipeline."})
            command = [sys.executable, "scripts/decide_review.py"]
            if decision == "approve":
                command.append("--approved")
            elif reason:
                command.extend(["--reason", reason])
            review = subprocess.run(
                command, cwd=pending["run_root"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                env=self.run_environment(pending["sources"], pending["run_root"]),
            )
            if review.returncode:
                return self.send_json(500, {"error": (review.stderr or review.stdout)[-5000:]})
            outcome = json.loads(review.stdout)
            result = pending["result"]
            result.update({
                "review_status": outcome.get("review_status"),
                "review_decision": json.loads((pending["run_root"] / "generated" / "review_decision.json").read_text(encoding="utf-8")),
                "human_review_status": "APPROVED" if decision == "approve" else "REJECTED",
            })
            result["run_id"] = None
            PENDING_RUNS.pop(run_id, None)
            pending["temp"].cleanup()
            return self.send_json(200, result)
        except subprocess.TimeoutExpired:
            return self.send_json(504, {"error": "Review decision timed out"})
        except Exception as error:
            return self.send_json(500, {"error": f"{type(error).__name__}: {error}"})

    def send_json(self, code, payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve() -> None:
    address = ("127.0.0.1", int(os.environ.get("FLOWFORGE_UI_PORT", "8766")))
    print(f"FlowForge integrated demo: http://{address[0]}:{address[1]}", flush=True)
    ThreadingHTTPServer(address, Handler).serve_forever()


if __name__ == "__main__":
    serve()
