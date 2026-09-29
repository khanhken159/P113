import json
from pathlib import Path

from agents.llm import generate_text
from agents.prompt_loader import load_system_prompt

ROOT = Path(__file__).resolve().parents[2]

PIPELINE_PATH = (
    ROOT
    / "generated"
    / "generated_pipeline.py"
)

BACKUP_PATH = (
    ROOT
    / "generated"
    / "generated_pipeline_before_optimizer.py"
)

TEST_REPORT_PATH = (
    ROOT
    / "generated"
    / "test_report.json"
)

OPTIMIZATION_REPORT_PATH = (
    ROOT
    / "generated"
    / "optimization_report.json"
)


def extract_python_code(text: str) -> str:
    """Bỏ Markdown nếu LLM trả về ```python ... ```."""

    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        if lines:
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines)

    return text.strip()


def deterministic_fix(
    pipeline_code: str,
    stderr: str,
) -> str:
    """
    Sửa một số lỗi phổ biến khi dùng provider mock
    hoặc khi không gọi được LLM.
    """

    fixed_code = pipeline_code

    # Sửa lỗi ngày chỉ chấp nhận %d/%m/%Y
    if "doesn't match format" in stderr:
        fixed_code = fixed_code.replace(
            '''pd.to_datetime(
        df["order_date"],
        format="%d/%m/%Y",
    )''',
            '''pd.to_datetime(
        df["order_date"],
        format="mixed",
        dayfirst=True,
        errors="coerce",
    )''',
        )

    return fixed_code


def optimize_with_llm(
    provider: str,
    pipeline_code: str,
    test_report: dict,
) -> str:
    prompt = f"""TEST REPORT:
{json.dumps(test_report, ensure_ascii=False, indent=2)}

CURRENT PIPELINE:
{pipeline_code}"""

    response = generate_text(
        provider, prompt, system_prompt=load_system_prompt("prompt_optimizer")
    )

    return extract_python_code(response)


def run(state: dict) -> dict:
    attempts = int(
        state.get(
            "optimization_attempts",
            0,
        )
    ) + 1

    artifacts = dict(
        state.get("artifacts", {})
    )

    if not PIPELINE_PATH.exists():
        return {
            "optimization_attempts": attempts,
            "optimizer_status": "FAILED",
            "status": "optimization_failed",
            "optimizer_error": (
                "Không tìm thấy generated_pipeline.py"
            ),
        }

    if not TEST_REPORT_PATH.exists():
        return {
            "optimization_attempts": attempts,
            "optimizer_status": "FAILED",
            "status": "optimization_failed",
            "optimizer_error": (
                "Không tìm thấy test_report.json"
            ),
        }

    pipeline_code = PIPELINE_PATH.read_text(
        encoding="utf-8"
    )

    test_report = json.loads(
        TEST_REPORT_PATH.read_text(
            encoding="utf-8"
        )
    )

    provider = state.get(
        "provider",
        "mock",
    )

    error_message = test_report.get(
        "stderr",
        "",
    )

    # Lưu code cũ để so sánh trước và sau.
    BACKUP_PATH.write_text(
        pipeline_code,
        encoding="utf-8",
    )

    try:
        if provider in {
            "openai",
            "gemini",
        }:
            optimized_code = optimize_with_llm(
                provider,
                pipeline_code,
                test_report,
            )

        else:
            optimized_code = deterministic_fix(
                pipeline_code,
                error_message,
            )

        if not optimized_code.strip():
            raise ValueError(
                "Optimizer trả về code rỗng"
            )

        # Kiểm tra cú pháp trước khi ghi đè.
        compile(
            optimized_code,
            str(PIPELINE_PATH),
            "exec",
        )

        changed = (
            optimized_code.strip()
            != pipeline_code.strip()
        )

        if not changed:
            raise ValueError(
                "Optimizer không tạo ra thay đổi nào"
            )

        PIPELINE_PATH.write_text(
            optimized_code,
            encoding="utf-8",
        )

        optimization_report = {
            "agent": "optimizer",
            "status": "OPTIMIZED",
            "attempt": attempts,
            "provider": provider,
            "changed": changed,
            "tester_error": error_message,
            "backup": str(BACKUP_PATH),
            "pipeline": str(PIPELINE_PATH),
        }

        optimizer_status = "OPTIMIZED"
        status = "optimized"

    except Exception as error:
        optimization_report = {
            "agent": "optimizer",
            "status": "FAILED",
            "attempt": attempts,
            "provider": provider,
            "changed": False,
            "tester_error": error_message,
            "optimizer_error": (
                f"{type(error).__name__}: {error}"
            ),
        }

        optimizer_status = "FAILED"
        status = "optimization_failed"

    OPTIMIZATION_REPORT_PATH.write_text(
        json.dumps(
            optimization_report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    artifacts["optimization_report"] = str(
        OPTIMIZATION_REPORT_PATH
    )

    artifacts["pipeline_backup"] = str(
        BACKUP_PATH
    )

    return {
        "artifacts": artifacts,
        "optimization_attempts": attempts,
        "optimizer_status": optimizer_status,
        "status": status,
    }
