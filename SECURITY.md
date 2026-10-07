# Security and synthetic reports

Never publish actual knowledge, conversations, logs, feedback, operational receipts, personal configuration, credentials, private endpoints, or anonymized extracts from them. An identifier removed from a real record does not make it a synthetic fixture. Recreate the suspected issue using new invented values and temporary files.

For a security concern, use GitHub's private vulnerability reporting when available. If it is unavailable, use another private channel chosen by the maintainer. A public issue may contain only a synthetic reproduction and public-source references; do not post exploit details that expose a user's data or access credentials.

The remote service fails closed without authentication or explicit loopback-only access. Public endpoints retain OAuth owner authorization, PKCE, resource binding, Host/Origin validation, revocation/replay checks and applicable rate/body limits. Static header tokens are not a substitute for hosted-client OAuth and do not grant public Funnel access by default. TOTP uses a privately configured seed, restrictive file permissions, failure lockout and replay prevention. No seed or credential is distributed.

The public copy changes device labels, icon representation, implicit deployment paths and schedules, not authentication policy. Deployment-only source publishing is excluded. Review these boundaries before exposing a new host or extending the candidate/evidence interfaces.

Tests patch `Path.home` and `os.path.expanduser` within the pytest process, clear inherited `ENGRAM_*` values, and use explicit temporary data/source/auth roots. HOME and CODEX_HOME environment variables are not changed. Business subprocesses inherit explicit synthetic paths; imports and deterministic tests use the existing interpreter's dependencies while loading Engram from this source tree. This is source-path/runtime isolation, not proof of a fresh installed environment or live deployment. Wheel installation checks must be reported separately.
