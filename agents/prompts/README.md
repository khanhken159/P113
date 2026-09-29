# Agent prompts

Each LLM-calling agent has a separately editable prompt file. The `prompt_<agent>.md` names are resolved by `agents/prompt_loader.py`.

- `prompt_plan.md`: Planner behavior for custom CSV schemas and built-in order sources.
- `prompt_coder.md`: one DuckDB SQL query generated from the validated plan.
- `prompt_clarifier.md`: when and how to ask for missing request details.
- `prompt_optimizer.md`: how to repair a pipeline from Tester feedback.

The Python agent passes run-specific information (request, schemas, plan, history, test report, or code) separately from the system prompt. Edit the matching prompt file to change model behavior; edit the Python agent to change deterministic mapping, validation, provider routing, or execution behavior.

The integrated UI defaults to `openai` (`FLOWFORGE_UI_PROVIDER` can select `openai`, `gemini`, or `mock`). Planner, Clarifier, SQL Coder, and Optimizer call the selected LLM provider; the SQL Coder returns one read-only DuckDB query, which the local runner inserts into its deterministic data-loading pipeline. `mock` uses deterministic templates. Tester, Acceptance, and Human Review are deterministic checks and do not call an LLM. The complete workflow still runs through Tester, Acceptance, and Reviewer.
