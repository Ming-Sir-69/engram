"""Fail-closed publication boundary for a deliberately prepared public source tree.

Only explicitly listed public UTF-8 files are read. Runtime trees are never inputs.
Reports contain risk categories and locations, never matching content.
Requires Python 3.11+; uses only the standard library.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlsplit

MANIFEST = "PUBLIC-FILES.json"
CONFIG = "PUBLICATION.toml"
MAX_FILES = 3000
MAX_BYTES = 2_000_000
DENIED_DIRS = frozenset({
    ".state", ".cache", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "logs", "log", "records", "runtime", "runtime-data", "runtime_data",
    "ops", "reports", "private", "artifacts", "work", "deliverables", "weights",
    "adapters", "checkpoints", "training-data", "training_data", "production-data",
})
DENIED_SUFFIXES = frozenset({
    ".jsonl", ".ndjson", ".log", ".db", ".sqlite", ".sqlite3", ".wal", ".shm",
    ".pkl", ".pickle", ".parquet", ".arrow", ".pt", ".pth", ".ckpt",
    ".safetensors", ".gguf", ".ggml", ".h5", ".hdf5", ".npy", ".npz",
    ".bin", ".zip", ".tar", ".gz", ".tgz", ".7z", ".pem", ".key", ".p12", ".pfx",
})
TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".json", ".yaml", ".yml",
    ".sh", ".bash", ".zsh", ".lock", ".cfg", ".ini", ".xml", ".html", ".css",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".svg", ".sql",
})
TEXT_NAMES = frozenset({"LICENSE", "NOTICE", "COPYING", "Makefile", ".gitignore", ".gitattributes", ".editorconfig", ".python-version"})
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
SYNTHETIC_UUIDS = frozenset({"00000000-0000-0000-0000-000000000000", "00000000-0000-4000-8000-000000000001"})
PERSONAL_PATH = re.compile(r"/(?:Users|home|Volumes)/[^\s\"'<>`]+|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"'<>`]+", re.IGNORECASE)
RECORD_ID = re.compile(r"\b(?:rec|record|memory|session|thread|task|transaction|event|feedback|receipt)[_-](?:[0-9a-f]{16,}|[0-9]{10,})(?:\b|_)", re.IGNORECASE)
CREDENTIALS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{15,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b|"
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
SECRET_ASSIGNMENT = re.compile(r"[\"']?\b(?:password|passwd|api[_-]?key|secret|access[_-]?token|auth[_-]?token|token)\b[\"']?\s*[:=]\s*([\"'])([^\n\"']+)\1", re.IGNORECASE)
PLACEHOLDERS = frozenset({"", "string", "str", "example", "dummy", "test", "synthetic", "REPLACE_ME", "YOUR_API_KEY", "YOUR_TOKEN"})
URL = re.compile(r"https?://[^\s<>\"'`\)]+", re.IGNORECASE)
PRIVATE_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".ts.net", ".ngrok.io", ".ngrok.app", ".ngrok-free.app", ".trycloudflare.com")
PUBLIC_HOSTS = frozenset({"github.com", "raw.githubusercontent.com", "docs.python.org", "pypi.org", "opensource.org", "spdx.org", "www.gnu.org", "choosealicense.com", "json-schema.org", "www.w3.org"})
SYNTHETIC_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32"))
EVOLUTION_SECTIONS = (
    ("Problem", "问题", "解决的问题"),
    ("Architecture and interface", "架构与接口", "架构/接口变化"),
    ("Decisions and alternatives", "决策与替代方案", "理由与被替换方案"),
    ("Research hypotheses", "研究假设", "可复用研究假设"),
    ("Validation and limits", "验证与边界", "验证方式及已知边界"),
    ("Next", "下一步", "下一方向"),
)
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def safe_location(path: str) -> str:
    """Do not echo an identifier accidentally placed in a filename."""
    path = UUID.sub("<identifier>", path)
    path = RECORD_ID.sub("<identifier>", path)
    path = CREDENTIALS.sub("<credential>", path)
    return path[:240]


@dataclass(frozen=True)
class Issue:
    category: str
    path: str = ""
    line: int | None = None

    def as_dict(self):
        return {"category": self.category, "path": safe_location(self.path), **({"line": self.line} if self.line else {})}


class AuditError(Exception):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("Public source boundary validation failed")

    def as_dict(self):
        return {"ok": False, "issues": [issue.as_dict() for issue in self.issues]}


@dataclass
class CheckedPayload:
    root: Path
    manifest: dict
    payload: dict[str, bytes] = field(repr=False)
    config: dict = field(repr=False)


def relative_path(value) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in "*?[]\n\r\x00"):
        return False
    parts = PurePosixPath(value).parts
    return not PurePosixPath(value).is_absolute() and value == PurePosixPath(value).as_posix() and all(p not in {".", "..", ".git"} for p in parts)


def inventory(root: Path):
    found = set()
    issues = []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError:
            issues.append(Issue("unreadable-directory", prefix))
            continue
        for child in children:
            relative = prefix + child.name
            # Root Git metadata belongs to the checkout, never the public payload.
            if not prefix and child.name == ".git":
                if child.is_symlink():
                    issues.append(Issue("symlink", relative))
                continue
            if child.is_symlink():
                issues.append(Issue("symlink", relative))
            elif child.is_dir(follow_symlinks=False):
                if child.name == ".git":
                    issues.append(Issue("nested-git", relative))
                elif child.name.lower() in DENIED_DIRS:
                    issues.append(Issue("forbidden-directory", relative))
                else:
                    pending.append((Path(child.path), relative + "/"))
            elif child.is_file(follow_symlinks=False):
                found.add(relative)
                if len(found) > MAX_FILES:
                    raise AuditError([Issue("too-many-files")])
            else:
                issues.append(Issue("special-file", relative))
    return found, issues


def read_text_file(root: Path, relative: str):
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise AuditError([Issue("unsafe-file", relative)])
    if path.stat().st_size > MAX_BYTES:
        raise AuditError([Issue("file-too-large", relative)])
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise AuditError([Issue("file-too-large", relative)])
        content = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise AuditError([Issue("non-text-or-unreadable-file", relative)])
    if "\x00" in content:
        raise AuditError([Issue("binary-file", relative)])
    return data, content


def placeholder(value: str) -> bool:
    return value in PLACEHOLDERS or bool(re.fullmatch(r"<[^<>\n]+>|\$\{[A-Z0-9_]+\}|(?:synthetic|example|dummy|test)-[A-Za-z0-9_-]{1,48}", value))


def url_risk(value, allowed_hosts):
    # Match only explicit symbolic host/port expressions, never a whole line.
    # Full content credential/identifier checks remain active for that line.
    authority = value.split("://", 1)[1].split("/", 1)[0]
    expression = r"\{[A-Za-z_][A-Za-z0-9_.]*(?:\[[0-9]+\])?\}"
    symbolic = bool(re.fullmatch(expression + r"(?::" + expression + r")?", authority))
    if symbolic:
        for key, token in parse_qsl(value.partition("?")[2]):
            if re.search(r"(?:token|secret|password|api[_-]?key|authorization)", key, re.IGNORECASE) and token and not placeholder(token):
                return "url-credential"
        return None
    local_port = re.fullmatch(r"(localhost|127\.0\.0\.1|\[::1\]):" + expression, authority)
    if local_port:
        value = value.replace(authority, local_port.group(1) + ":1", 1)
    try:
        parsed = urlsplit(value.rstrip(".,;"))
        host = (parsed.hostname or "").lower()
        if parsed.username is not None or parsed.password is not None:
            return "url-credential"
        for key, token in parse_qsl(parsed.query):
            if re.search(r"(?:token|secret|password|api[_-]?key|authorization)", key, re.IGNORECASE) and token and not placeholder(token):
                return "url-credential"
        if host in {"example.com", "example.org", "example.net"} or host.endswith((".example", ".invalid", ".test", ".example.com", ".example.org", ".example.net")):
            return None
        # Generic local service endpoints are implementation, not a private host.
        if host in {"localhost", "127.0.0.1", "::1"}:
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address:
            if any(address in network for network in SYNTHETIC_NETWORKS if address.version == network.version):
                return None
            return "private-host-url" if not address.is_global else "unapproved-public-url"
        if host == "localhost" or "." not in host or host.endswith(PRIVATE_HOST_SUFFIXES):
            return "private-host-url"
        if host not in allowed_hosts:
            return "unapproved-public-url"
    except ValueError:
        return "invalid-url"
    return None


def scan_content(relative, content, allowed_hosts):
    issues = []
    for number, line in enumerate(content.splitlines(), 1):
        categories = set()
        if PERSONAL_PATH.search(line):
            categories.add("personal-absolute-path")
        if RECORD_ID.search(line) or any(match.group().lower() not in SYNTHETIC_UUIDS for match in UUID.finditer(line)):
            categories.add("record-identifier")
        if CREDENTIALS.search(line):
            categories.add("credential")
        if any(not placeholder(match.group(2)) for match in SECRET_ASSIGNMENT.finditer(line)):
            categories.add("literal-secret")
        for match in URL.finditer(line):
            category = url_risk(match.group(), allowed_hosts)
            if category:
                categories.add(category)
        issues.extend(Issue(category, relative, number) for category in sorted(categories))
    return issues


def lookup(document, dotted):
    value = document
    for component in dotted.split("."):
        value = value[component]
    return value


def synthetic_config_matches(value, schema):
    """Small closed config validator, deliberately not a general JSON Schema engine.

    Every object must reject extra keys; strings must be enumerated/constants.
    Arrays and raw record-bearing keys are not supported for config exceptions.
    All file text is still scanned by the non-bypassable content rules.
    """
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        record_keys = {"body", "records", "events", "messages", "memory", "feedback", "context", "record_id", "session_id", "thread_id", "transaction_id", "task_id"}
        return (
            isinstance(value, dict) and isinstance(properties, dict) and schema.get("additionalProperties") is False
            and isinstance(required, list) and set(required) <= set(value) <= set(properties)
            and not record_keys & set(value)
            and all(synthetic_config_matches(item, properties[key]) for key, item in value.items())
        )
    types = {"string": str, "boolean": bool, "integer": int, "number": (int, float), "null": type(None)}
    if kind not in types or not isinstance(value, types[kind]) or (kind in {"integer", "number"} and isinstance(value, bool)):
        return False
    if "const" in schema:
        return type(value) is type(schema["const"]) and value == schema["const"]
    if "enum" in schema:
        return isinstance(schema["enum"], list) and any(type(value) is type(item) and value == item for item in schema["enum"])
    if kind == "string":
        return False
    if kind in {"integer", "number"}:
        return "minimum" in schema and "maximum" in schema and schema["minimum"] <= value <= schema["maximum"]
    return True


def version_from(data, key, path):
    try:
        value = lookup(tomllib.loads(data.decode("utf-8")), key)
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise AuditError([Issue("version-source-invalid", path)])
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise AuditError([Issue("version-not-semver", path)])
    return value


def is_newer(current, previous):
    left, right = SEMVER.fullmatch(current), SEMVER.fullmatch(previous)
    a, b = tuple(map(int, left.group(1, 2, 3))), tuple(map(int, right.group(1, 2, 3)))
    if a != b:
        return a > b
    x, y = left.group(4), right.group(4)
    if x is None or y is None:
        return x is None and y is not None
    for p, q in zip(x.split("."), y.split(".")):
        if p == q:
            continue
        if p.isdigit() and q.isdigit():
            return int(p) > int(q)
        if p.isdigit() != q.isdigit():
            return not p.isdigit()
        return p > q
    return len(x.split(".")) > len(y.split("."))


def changelog_section(content, version):
    lines = content.splitlines(keepends=True)
    start = None
    for number, line in enumerate(lines):
        if re.fullmatch(r"##\s+\[" + re.escape(version) + r"\]\s+-\s+\d{4}-\d{2}-\d{2}\s*", line.strip()):
            if start is not None:
                raise AuditError([Issue("release-notes-duplicate")])
            start = number
    if start is None:
        return None
    end = next((n for n in range(start + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    section = "".join(lines[start:end]).strip() + "\n"
    return section if len("".join(lines[start + 1:end]).strip()) >= 30 else None


def git_blob(root, commit, relative):
    completed = subprocess.run(["git", "-C", str(root), "show", commit + ":" + relative], capture_output=True, check=False)
    return completed.stdout if completed.returncode == 0 else None


def baseline_check(root, payload, config, version, base_ref):
    resolved = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", base_ref + "^{commit}"], capture_output=True, text=True, check=False)
    if resolved.returncode:
        raise AuditError([Issue("git-baseline-unavailable")])
    commit = resolved.stdout.strip()
    old_version = git_blob(root, commit, config["version_file"])
    if old_version is None:
        raise AuditError([Issue("baseline-version-missing", config["version_file"])])
    previous = version_from(old_version, config["version_key"], config["version_file"])
    candidates = set(payload)
    old_manifest = git_blob(root, commit, MANIFEST)
    if old_manifest:
        try:
            old_files = json.loads(old_manifest)["files"]
            if not isinstance(old_files, list) or not all(relative_path(p) for p in old_files):
                raise ValueError
            candidates.update(old_files)
        except (ValueError, KeyError, TypeError):
            raise AuditError([Issue("baseline-manifest-invalid", MANIFEST)])
    changed = sum(payload.get(path) != git_blob(root, commit, path) for path in candidates)
    if changed and not is_newer(version, previous):
        raise AuditError([Issue("version-not-advanced", config["version_file"])])
    if changed:
        evolution = config["evolution_dir"] + "/" + version + ".md"
        unchanged = [Issue("version-notes-unchanged", path) for path in (config["changelog"], evolution) if payload[path] == git_blob(root, commit, path)]
        if unchanged:
            raise AuditError(unchanged)
    return changed


def check(root, base_ref=None):
    requested_root = Path(root).expanduser()
    if requested_root.is_symlink():
        raise AuditError([Issue("symlink")])
    root = requested_root.resolve()
    if not root.is_dir():
        raise AuditError([Issue("public-root-missing")])
    # Fail before walking an accidentally selected private/runtime directory.
    if not (root / MANIFEST).is_file():
        raise AuditError([Issue("manifest-missing", MANIFEST)])
    found, issues = inventory(root)
    if MANIFEST not in found:
        raise AuditError(issues + [Issue("manifest-missing", MANIFEST)])
    _, text = read_text_file(root, MANIFEST)
    try:
        document = json.loads(text)
        listed = document["files"]
        if set(document) != {"schema", "files"} or document["schema"] != 1 or not isinstance(listed, list) or not all(relative_path(p) for p in listed) or len(set(listed)) != len(listed):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise AuditError(issues + [Issue("manifest-invalid", MANIFEST)])
    allowed = set(listed)
    issues.extend(Issue("unlisted-file", path) for path in sorted(found - allowed))
    issues.extend(Issue("listed-file-missing", path) for path in sorted(allowed - found))
    for path in sorted(allowed):
        parts = PurePosixPath(path).parts
        basename = parts[-1]
        suffix = PurePosixPath(path).suffix.lower()
        if any(p.lower() in DENIED_DIRS for p in parts[:-1]):
            issues.append(Issue("forbidden-directory", path))
        if suffix in DENIED_SUFFIXES or basename == ".env" or basename.startswith(".env."):
            issues.append(Issue("forbidden-file-type", path))
        elif suffix not in TEXT_SUFFIXES and basename not in TEXT_NAMES:
            issues.append(Issue("unsupported-file-type", path))
        issues.extend(scan_content(path, path, PUBLIC_HOSTS))
    if issues:
        raise AuditError(issues)
    if CONFIG not in allowed or MANIFEST not in allowed:
        raise AuditError([Issue("publication-controls-not-listed")])
    _, config_text = read_text_file(root, CONFIG)
    try:
        config = tomllib.loads(config_text)["publication"]
        required = {"schema", "version_file", "version_key", "changelog", "evolution_dir"}
        accepted = required | {"source_roots", "public_url_hosts", "synthetic_config_files"}
        if not required <= set(config) or set(config) - accepted or config["schema"] != 1:
            raise ValueError
        if not all(relative_path(config[key]) for key in ("version_file", "changelog", "evolution_dir")) or not isinstance(config["version_key"], str):
            raise ValueError
        hosts = config.get("public_url_hosts", [])
        if not isinstance(hosts, list) or any(not isinstance(h, str) or not re.fullmatch(r"[a-z0-9.-]+", h) or "." not in h or h.endswith(PRIVATE_HOST_SUFFIXES) for h in hosts):
            raise ValueError
        synthetic_configs = config.get("synthetic_config_files", {})
        if not isinstance(synthetic_configs, dict) or any(not relative_path(path) or not path.endswith(".json") or not relative_path(schema) or not schema.endswith(".schema.json") or path not in allowed or schema not in allowed for path, schema in synthetic_configs.items()):
            raise ValueError
    except (ValueError, KeyError, TypeError, tomllib.TOMLDecodeError):
        raise AuditError([Issue("publication-config-invalid", CONFIG)])
    payload = {}
    texts = {}
    for path in sorted(allowed):
        payload[path], texts[path] = read_text_file(root, path)
        issues.extend(scan_content(path, texts[path], PUBLIC_HOSTS | set(hosts)))
        if path.endswith(".svg") and re.search(r"<(?:script|foreignObject|iframe|object|embed)\b|<!DOCTYPE|<!ENTITY|\bon[a-z]+\s*=|\b(?:href|src)\s*=\s*[\"'](?:https?:|file:|data:|javascript:)", texts[path], re.IGNORECASE):
            issues.append(Issue("active-or-external-svg", path))
        if path.endswith(".schema.json"):
            try:
                schema = json.loads(texts[path])
                if not isinstance(schema, dict) or "$schema" not in schema or not {"type", "properties", "$defs", "definitions", "anyOf", "oneOf", "allOf"} & set(schema):
                    raise ValueError
            except (ValueError, TypeError):
                issues.append(Issue("json-schema-invalid", path))
    for path in sorted(allowed):
        if path.endswith(".json") and path != MANIFEST and not path.endswith(".schema.json"):
            if path not in synthetic_configs:
                issues.append(Issue("forbidden-json-data", path))
            else:
                try:
                    value = json.loads(texts[path])
                    schema = json.loads(texts[synthetic_configs[path]])
                    if not synthetic_config_matches(value, schema):
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    issues.append(Issue("synthetic-config-schema-mismatch", path))
    if config["version_file"] not in payload or config["changelog"] not in payload:
        issues.append(Issue("version-controls-not-listed"))
    if issues:
        raise AuditError(issues)
    version = version_from(payload[config["version_file"]], config["version_key"], config["version_file"])
    evolution = config["evolution_dir"] + "/" + version + ".md"
    if changelog_section(texts[config["changelog"]], version) is None:
        issues.append(Issue("release-notes-missing", config["changelog"]))
    if evolution not in texts:
        issues.append(Issue("evolution-not-listed", evolution))
    else:
        headings = [re.sub(r"^#{1,6}\s+", "", line).strip().casefold() for line in texts[evolution].splitlines() if re.match(r"^#{1,6}\s+", line)]
        if not all(any(option.casefold() in headings for option in alternatives) for alternatives in EVOLUTION_SECTIONS):
            issues.append(Issue("evolution-sections-missing", evolution))
    if issues:
        raise AuditError(issues)
    changed = baseline_check(root, payload, config, version, base_ref) if base_ref else None
    manifest = {
        "schema": 1, "version": version, "files": [
            {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in sorted(payload.items())
        ], "changed_public_files": changed,
    }
    return CheckedPayload(root, manifest, payload, config)


def external_output(result, path):
    output = Path(path).expanduser().resolve()
    if output == result.root or output.is_relative_to(result.root):
        raise AuditError([Issue("output-inside-public-source")])
    return output


def build(result, output):
    output = external_output(result, output)
    if output.exists():
        raise AuditError([Issue("payload-output-already-exists")])
    output.mkdir(parents=True)
    for relative, data in result.payload.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return result.manifest


def release_notes(result):
    return changelog_section(result.payload[result.config["changelog"]].decode("utf-8"), result.manifest["version"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "build", "release-notes"))
    parser.add_argument("--root", default=".", help="Prepared public source tree only")
    parser.add_argument("--base-ref", help="Git commit/ref for mandatory maintenance version comparison")
    parser.add_argument("--output", help="New external payload directory (build) or new release-notes file")
    parser.add_argument("--manifest-out", help="New external JSON manifest receipt; never a runtime snapshot")
    args = parser.parse_args(argv)
    try:
        result = check(args.root, args.base_ref)
        if args.command == "build":
            if not args.output:
                parser.error("build requires --output")
            build(result, args.output)
        elif args.command == "release-notes":
            notes = release_notes(result)
            if args.output:
                target = external_output(result, args.output)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x", encoding="utf-8") as handle:
                    handle.write(notes)
            else:
                sys.stdout.write(notes)
        if args.manifest_out:
            target = external_output(result, args.manifest_out)
            if args.command == "build" and target.is_relative_to(Path(args.output).expanduser().resolve()):
                raise AuditError([Issue("manifest-inside-release-payload")])
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as handle:
                json.dump(result.manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        if args.command != "release-notes":
            print(json.dumps({"ok": True, "public_manifest": result.manifest}, ensure_ascii=False, indent=2))
        return 0
    except AuditError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({"ok": False, "issues": [{"category": "io-or-git-error"}]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
