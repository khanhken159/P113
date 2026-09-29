from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def load_system_prompt(name: str) -> str:
    """Load one agent prompt from agents/prompts/prompt_<agent>.md."""
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"System prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()
