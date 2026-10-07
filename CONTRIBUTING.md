# Contributing and publication boundaries

Use invented, visibly synthetic fixtures. Do not attach actual records, memory, conversations, feedback, logs, operational receipts, credentials, personal configuration, or anonymized extracts from them. Recreate the failure as a new synthetic scenario.

Run the complete public suite (`uv run pytest`) and lint (`uv run ruff check .`) without downloading models. Keep all temporary data, OAuth state, exports and caches outside the checkout; tests isolate these paths. Real MLX evaluation is a separate task and cannot be inferred from deterministic offline vectors.

Every behavioral or architectural release must update package/service versions as applicable, CHANGELOG, evolution, and the relevant architecture/research documentation. State the problem, final behavior, rationale, falsifiable hypothesis, engineering evidence, and unresolved real-benefit claims. Candidate research does not become an implemented feature merely by appearing in an issue or plan.

Publish from a reviewed file allowlist, not by synchronizing a working machine. Review file types, symlinks, nested repositories, secrets, private paths/URLs/identifiers and data boundaries. A private audit manifest may record source hashes and cleaning categories; operational audit files must remain outside the public payload. Reviewers must agree before merge/tag/release. Only public source archives and newly written public release descriptions belong in a release.

Continuous maintenance means applying this version/documentation/review gate to future changes. It does not authorize background collection, machine-wide upload, or automatic publication of runtime material.

Maintain existing authentication, owner consent, PKCE, resource binding, credential-file checks and concurrency/rollback protections. A simpler example is not a reason to remove an access boundary.
