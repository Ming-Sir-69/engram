"""Fixed, bounded Engram maintenance evidence owned by the Engram MCP.

The handles, projections, bounds and evidence hashes form a fixed read-only contract.

No remote path/URL, shell, credentials, candidate bodies, or mutation surface.
Runtime receipts are schema projections, not arbitrary private-file reads.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class AccessDenied(ValueError):
    """A fixed evidence handle may not cross credential or descriptor boundaries."""


def redact(text):
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        lambda m: (
            "[PRIVATE KEY REDACTED]" + "".join(re.findall(r"\r\n|\r|\n", m.group(0)))
        ),
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{18,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
        "[TOKEN REDACTED]",
        text,
    )
    return re.sub(
        r"""(?im)(["']?(?:[A-Z0-9_]*_)?(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization)["']?\s*[:=]\s*)[^\r\n,}]+""",
        r"\1[REDACTED]",
        text,
    )


class FixedEvidenceAccess:
    """Owned evidence files only; the MCP cannot supply filesystem paths."""

    def __init__(self, deny_paths=()):
        self.denied = tuple(Path(p).absolute() for p in deny_paths)

    @contextmanager
    def opened(self, path, directory=False):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            raise AccessDenied("Fixed evidence requires an absolute pinned path")
        if any(path == p or p in path.parents for p in self.denied):
            raise AccessDenied("Evidence path excluded by operator policy")
        for part in path.parts:
            lower = part.lower()
            if (
                lower
                in {
                    ".ssh",
                    ".aws",
                    ".gnupg",
                    ".kube",
                    ".env",
                    "auth.json",
                    "credentials.json",
                    ".netrc",
                    ".npmrc",
                    ".pypirc",
                    "id_rsa",
                    "id_ed25519",
                }
                or lower.startswith(".env.")
                or lower.endswith((".key", ".pem", ".p12", ".pfx", ".keychain-db"))
            ):
                raise AccessDenied("Credential file is excluded")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if path.is_symlink():
                raise AccessDenied("Evidence resource cannot be a symlink") from exc
            raise
        try:
            info = os.fstat(fd)
            if directory and not stat.S_ISDIR(info.st_mode):
                raise AccessDenied("Expected evidence directory")
            if not directory and not stat.S_ISREG(info.st_mode):
                raise AccessDenied("Evidence must be a regular file")
            yield fd, path, info
        finally:
            os.close(fd)


ENGRAM_ROOT = Path(__file__).resolve().parents[2]
MAX_INPUT = 2 * 1024 * 1024
MAX_PAGE = 24000
MAX_REPORTS = 2000
REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9TZ.-]+\.json$")
SAFE_KEYS = frozenset(
    [
        "status",
        "reason",
        "research",
        "changes",
        "next_step",
        "run_receipt",
        "stage",
        "finished_at",
        "started_at",
        "updated_at",
        "next_retry_at",
        "last_attempt",
        "attempts",
        "consecutive_failures",
        "last_research_at",
        "last_result",
        "evidence_kind",
        "memory_benefit",
        "counterexamples",
        "unresolved",
        "title",
        "url",
        "source",
        "date",
        "conclusion",
        "hypothesis",
        "limitations",
        "applicability",
        "evidence",
        "at",
        "event",
        "task",
        "slot",
        "executor",
        "route",
        "exit",
        "skipped",
        "applied",
        "verified",
        "operation",
        "count",
        "error",
        "error_type",
        "error_code",
        "summary",
        "results",
        "tests",
        "test_count",
        "passed",
        "failed",
        "duration_seconds",
        "decision",
        "mode",
        "enabled",
        "version",
        "content_maintenance",
        "source_improvement",
        "claude",
        "owner",
        "scheduler",
        "schedule",
        "timezone",
        "execution_mode",
        "notification_policy",
        "purpose",
        "endpoints",
        "interval_hours",
        "max_attempts",
        "retry_hours",
        "automatic_execution",
        "manual_only",
        "cloud_gpt",
        "requires_mac_online",
        "requires_codex_app",
        "policy_version",
        "updated_by",
        "authentication",
        "diagnostics",
        "knowledge_writes",
        "code_changes",
        "controller",
        "content",
        "source",
        "primary_executor",
        "fallback_executor",
        "autonomous_enabled",
        "rationale",
        "allowed_executors",
        "disabled_executors",
        "changed_at",
        "last_success",
        "dependencies",
        "cloud_task_id",
        "clouds",
        "can_apply",
        "maintenance",
        "review",
        "research_mode",
        "state",
        "completion",
        "queued",
        "executed_at",
        "next_run_at",
        "request_id",
        "blocked_reason",
        "required_evidence",
        "regression",
        "verification",
        "review_limitation",
        "deployment",
        "started_epoch",
        "worker_pid",
        "exit_code",
        "returncode",
        "success",
        "ok",
        "events",
        "phase",
        "task",
        "run_id",
        "at",
        "slot",
        "detail",
        "schedule_definitions",
        "activated_at",
        "schedule_activated_at",
        "cloud_daily",
        "weekly",
        "midweek",
        "manual",
        "model",
        "reasoning",
        "runtime",
        "cloud_schedule_status",
        "cloud_tools_refreshed",
    ]
)
SENSITIVE_KEY = re.compile(
    r"(?i)(password|secret|credential|authorization|api.?key|access.?token|refresh.?token)"
)


def safe_projection(value, depth=0):
    """Drop unknown keys, raw model output, knowledge payloads and all secrets."""
    if depth > 8:
        return "[depth limited]"
    if isinstance(value, dict):
        return {
            key: safe_projection(item, depth + 1)
            for key, item in value.items()
            if key in SAFE_KEYS and not SENSITIVE_KEY.search(key)
        }
    if isinstance(value, list):
        return [safe_projection(item, depth + 1) for item in value[:100]]
    if isinstance(value, str):
        # Apply to the complete field before any clipping/pagination.
        return redact(re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", value))[:8000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccessDenied("Health probe redirects are not permitted")


class MaintenanceEvidence:
    def __init__(self, access=None, project=ENGRAM_ROOT, home=None):
        self.access = access or FixedEvidenceAccess()
        self.project = Path(project).absolute()
        self.home = Path(home or Path.home()).absolute()
        self.content_state = self.home / ".local/state/engram-maintenance"
        self.source_state = self.home / ".local/state/engram-self-improvement"
        self.cloud_source_state = self.home / ".local/state/engram-cloud-source"
        self.reports = self.project / "ops/self-improvement/reports"
        self.fixed = {
            "policy": (
                "all",
                self.project / "ops/maintenance/cloud-policy.json",
                "json",
            ),
            "content.runner": (
                "content",
                self.project / "ops/maintenance/runner.py",
                "source",
            ),
            "content.state": ("content", self.content_state / "state.json", "json"),
            "content.status": ("content", self.content_state / "status.json", "json"),
            "content.runs": ("content", self.content_state / "runs.jsonl", "jsonl"),
            "cloud.receipts": (
                "content",
                self.home / ".local/share/engram-mcp/observations/cloud-runs.json",
                "json",
            ),
            "source.runner": (
                "source",
                self.project / "ops/self-improvement/runner.py",
                "source",
            ),
            "source.control": (
                "source",
                self.project / "ops/self-improvement/control.py",
                "source",
            ),
            "source.readme": (
                "source",
                self.project / "ops/self-improvement/README.md",
                "source",
            ),
            "source.state": ("source", self.source_state / "state.json", "json"),
            "source.status": ("source", self.source_state / "status.json", "json"),
            "cloud_source.status": (
                "source",
                self.cloud_source_state / "status.json",
                "json",
            ),
            "cloud.guide": (
                "all",
                self.project / "integrations/engram-cloud-monitoring.md",
                "source",
            ),
            "cloud.weekly_prompt": (
                "content",
                self.project / "integrations/chatgpt/weekly-saved-prompt.txt",
                "source",
            ),
            "cloud.midweek_prompt": (
                "content",
                self.project / "integrations/chatgpt/midweek-saved-prompt.txt",
                "source",
            ),
        }

    def _opened(self, path, directory=False):
        """Reuse normal credential policy and enforce the exact pinned path too."""
        return self.access.opened(path, directory=directory)

    @staticmethod
    def _verify_descriptor(fd, expected):
        if sys.platform == "darwin":
            actual = os.fsdecode(fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0])
        elif sys.platform.startswith("linux"):
            actual = os.readlink(f"/proc/self/fd/{fd}")
        else:
            raise AccessDenied("Unverified platform")
        # Unlike general read tools this lane rejects symlink aliases even when
        # the target would otherwise be readable under the broad user policy.
        if Path(actual) != expected:
            raise AccessDenied("Maintenance resource must be its exact registered path")

    def _bytes(self, path, tail=False):
        with self._opened(path) as (fd, _, st):
            self._verify_descriptor(fd, path)
            start = max(0, st.st_size - MAX_INPUT) if tail else 0
            if st.st_size > MAX_INPUT and not tail:
                raise ValueError("Maintenance input exceeds 2 MiB")
            if start:
                os.lseek(fd, start, os.SEEK_SET)
            blocks = []
            remaining = MAX_INPUT + 1
            while remaining:
                block = os.read(fd, min(65536, remaining))
                if not block:
                    break
                blocks.append(block)
                remaining -= len(block)
            raw = b"".join(blocks)
            if len(raw) > MAX_INPUT:
                raise ValueError("Maintenance input changed beyond limit")
            if start:
                raw = raw.split(b"\n", 1)[-1]
            return raw, st, bool(start)

    def _report_names(self):
        try:
            with self._opened(self.reports, directory=True) as (fd, _, _):
                self._verify_descriptor(fd, self.reports)
                names = []
                count = 0
                with os.scandir(fd) as entries:
                    for entry in entries:
                        count += 1
                        if count > MAX_REPORTS:
                            raise ValueError(
                                "Report catalog exceeds 2000 entries; archive locally"
                            )
                        if REPORT_NAME.fullmatch(entry.name) and entry.is_file(
                            follow_symlinks=False
                        ):
                            names.append(entry.name)
                return sorted(names, reverse=True)
        except FileNotFoundError:
            return []

    def _resolve(self, resource):
        if resource in self.fixed:
            return self.fixed[resource]
        if resource.startswith("source.report:"):
            name = resource.removeprefix("source.report:")
            if REPORT_NAME.fullmatch(name):
                return "source", self.reports / name, "json"
        raise AccessDenied("Unknown maintenance resource; use the catalog handles")

    def _view(self, resource):
        chain, path, kind = self._resolve(resource)
        raw, st, tail = self._bytes(path, tail=kind == "jsonl")
        if kind == "source":
            view = redact(raw.decode("utf-8"))
            value = None
        elif kind == "jsonl":
            # Raw subprocess result text is deliberately not a safe key.
            # Return at most the latest 100 complete structured receipts.
            lines = raw.splitlines()
            value = [
                safe_projection(json.loads(line))
                for line in lines[-100:]
                if line.strip()
            ]
            tail = tail or len(lines) > 100
            view = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            source = json.loads(raw)
            if (
                resource == "cloud.receipts"
                and isinstance(source, dict)
                and isinstance(source.get("events"), list)
            ):
                tail = len(source["events"]) > 100
                source["events"] = source["events"][-100:]
            value = safe_projection(source)
            view = json.dumps(value, ensure_ascii=False, indent=2)
        return (
            view,
            value,
            {
                "resource": resource,
                "chain": chain,
                "path": str(path),
                "mtime_ns": st.st_mtime_ns,
                "source_size_bytes": st.st_size,
                "view_sha256": hashlib.sha256(view.encode("utf-8")).hexdigest(),
                "view": "redacted_source"
                if kind == "source"
                else "safe_field_projection",
                "source_sampled": tail,
                "projection_limits": None
                if kind == "source"
                else {
                    "allowed_keys_only": True,
                    "max_string_chars": 8000,
                    "max_list_items": 100,
                    "max_depth": 8,
                },
            },
        )

    def catalog(self, chain="all", offset=0, limit=30):
        if chain not in {"all", "content", "source"}:
            raise ValueError("chain must be all, content or source")
        if not 0 <= offset <= 10000 or not 1 <= limit <= 100:
            raise ValueError("offset 0..10000 and limit 1..100 required")
        resources = [
            key
            for key, row in self.fixed.items()
            if chain == "all" or row[0] in {chain, "all"}
        ]
        if self._snapshot("policy").get("data", {}).get("mode") == "cloud_gpt":
            resources = [
                key
                for key in resources
                if key not in {"content.state", "content.status"}
            ]
        if chain != "content":
            resources += ["source.report:" + name for name in self._report_names()]
        selected = resources[offset : offset + limit]
        rows = []
        for resource in selected:
            row_chain, path, kind = self._resolve(resource)
            row = {
                "resource": resource,
                "chain": row_chain,
                "path": str(path),
                "view": "redacted_source"
                if kind == "source"
                else "safe_field_projection",
            }
            try:
                with self._opened(path) as (fd, _, st):
                    self._verify_descriptor(fd, path)
                    row.update(available=True, mtime_ns=st.st_mtime_ns)
            except (OSError, ValueError) as exc:
                row.update(available=False, error_type=type(exc).__name__)
            rows.append(row)
        end = offset + len(selected)
        return {
            "resources": rows,
            "offset": offset,
            "next_offset": end if end < len(resources) else None,
            "total": len(resources),
            "policy": "Read-only; receipts omit unknown/private fields. Candidate knowledge belongs to Engram MCP.",
        }

    def read(self, resource, offset=0, length=12000, expected_sha256=None):
        if not 0 <= offset <= 4 * MAX_INPUT or not 1 <= length <= MAX_PAGE:
            raise ValueError("offset 0..8388608 and length 1..24000 required")
        view, _, metadata = self._view(resource)
        if expected_sha256 and expected_sha256 != metadata["view_sha256"]:
            return {
                **metadata,
                "status": "source_changed",
                "text": None,
                "next_offset": 0,
                "reason": "Restart pagination using the new view hash",
            }
        end = min(len(view), offset + length)
        return {
            **metadata,
            "text": view[offset:end],
            "offset": offset,
            "offset_unit": "unicode_codepoints",
            "total_chars": len(view),
            "next_offset": end if end < len(view) else None,
            "truncated": end < len(view),
            "untrusted_data": True,
        }

    def _snapshot(self, resource):
        try:
            _, value, metadata = self._view(resource)
            return {**metadata, "available": True, "data": value}
        except (OSError, ValueError) as exc:
            return {
                "resource": resource,
                "available": False,
                "error_type": type(exc).__name__,
            }

    def _health(self):
        # This is an exact loopback health URL, never a caller-controlled URL.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        try:
            with opener.open("http://127.0.0.1:8768/healthz", timeout=2) as response:
                raw = response.read(4097)
                if len(raw) > 4096:
                    raise ValueError("Oversized health reply")
                value = json.loads(raw)
                return {
                    "reachable": True,
                    "ok": value.get("ok") is True,
                    "evidence": "loopback_health_only",
                    "cloud_oauth_tool_call_verified": False,
                }
        except (OSError, ValueError) as exc:
            return {
                "reachable": False,
                "ok": None,
                "error_type": type(exc).__name__,
                "evidence": "loopback_health_only",
                "cloud_oauth_tool_call_verified": False,
            }

    def _cloud_source_snapshot(self):
        result = self._snapshot("cloud_source.status")
        if result.get("error_type") != "FileNotFoundError":
            return result
        requests = self.cloud_source_state / "requests"
        try:
            with self._opened(requests, directory=True) as (fd, _, _):
                self._verify_descriptor(fd, requests)
                with os.scandir(fd) as entries:
                    has_request = next(entries, None) is not None
        except FileNotFoundError:
            has_request = False
        except (OSError, ValueError):
            has_request = True  # Unknown state cannot be called not_started.
        if not has_request:
            return {
                "resource": "cloud_source.status",
                "available": False,
                "status": "not_started",
                "reason": "no_status_or_staged_request_observed",
            }
        return {
            **result,
            "status": "unknown",
            "reason": "request_exists_but_status_unavailable",
        }

    def status(self):
        policy = self._snapshot("policy")
        cloud_mode = policy.get("data", {}).get("mode") == "cloud_gpt"
        content = self._snapshot("content.runs")
        if content.get("available"):
            content["data"] = content["data"][-1:]
        cloud_receipts = self._snapshot("cloud.receipts")
        daily = [
            event
            for event in cloud_receipts.get("data", {}).get("events", [])
            if isinstance(event, dict) and event.get("task") == "cloud_daily"
        ]
        cloud_status = {
            key: value for key, value in cloud_receipts.items() if key != "data"
        }
        cloud_status.update(
            data=daily[-1] if daily else None,
            status=daily[-1].get("phase", "unknown") if daily else "unknown",
        )
        if cloud_receipts.get("available") and not daily:
            cloud_status.update(
                status="not_started", reason="no_cloud_daily_receipt_observed"
            )
        inactive = {
            "status": "not_applicable",
            "reason": "local_runner_inactive_by_cloud_policy",
        }
        try:
            reports = self._report_names()
            latest_report = (
                self._snapshot("source.report:" + reports[0]) if reports else None
            )
        except (OSError, ValueError) as exc:
            latest_report = {"available": False, "error_type": type(exc).__name__}
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "mcp": {"service": "engram", "tool_call_succeeded": True},
            "engram": self._health(),
            "policy": policy,
            "content_maintenance": {
                "mode": "cloud_gpt" if cloud_mode else "legacy",
                "status": cloud_status
                if cloud_mode
                else self._snapshot("content.status"),
                "state": inactive if cloud_mode else self._snapshot("content.state"),
                "latest_cloud_receipt": cloud_status,
                "latest_legacy_receipt": content,
            },
            "source_improvement": {
                "mode": "legacy",
                "active": False if cloud_mode else None,
                "policy_evidence": "policy.mode=cloud_gpt" if cloud_mode else "unknown",
                "status": self._snapshot("source.status"),
                "state": self._snapshot("source.state"),
                "latest_receipt": latest_report,
            },
            "cloud_source": {
                "mode": "cloud_gpt" if cloud_mode else "unknown",
                "status": self._cloud_source_snapshot(),
            },
            "limits": "Snapshots can be stale. Missing is unknown, not failure. No scheduler/process liveness or memory-quality benefit is inferred. Inspect next_retry_at; pre-publish failure is not rollback. Cloud candidate receipts are read through Engram MCP.",
        }
