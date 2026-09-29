# Clarifier contract

Clarifier is a renderer for questions approved by the Ambiguity Resolver. It must not invent new questions or ask again for a fact already present in `resolved_business_rules`.

The Resolver profiles local schemas and sample values before classifying an issue:

- `DETERMINISTIC`: apply only when schema, datatype, filename metadata, or values establish the result. Record evidence in `resolved_automatically`.
- `DEFAULTABLE`: apply a configured default and record it in `defaults_used`.
- `BUSINESS_AMBIGUOUS`: ask only when multiple plausible choices change the business result and neither evidence nor a configured default settles it.

Ask a single combined question for related unresolved choices where possible. Never infer that `delivered`, `returned`, `failed`, or another status means completed/cancelled without a confirmed rule. Never infer a deduplication key from `order_id` until the data grain is established. For unknown status values, preserve and report them according to the configured default.

The Clarifier receives `ambiguity_resolution.questions_for_user` and matching `clarification_fields`. Render those items as supplied and stop the run until the answers are provided. Return only valid JSON when a model response is required.

Revenue / Sales clarification separates business policy (gross/net, eligible states, currency and refunds) from `revenue_roles_by_file` schema bindings. A Human role binding is JSON for the current dataset; it is not a global column alias. Preserve the answer's meaning and scope. Ask the supplied question when transaction value, event time or grain is unproven; do not infer a recognition policy from a payment or completion label.
