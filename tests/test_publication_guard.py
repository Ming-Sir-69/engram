"""Publication-boundary tests. Every fixture is created synthetically in a temp tree."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publication_check.py"
if not SCRIPT.is_file():
    SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "publication_check.py"
SPEC = importlib.util.spec_from_file_location("publication_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
import sys

sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class PublicationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "public"
        self.root.mkdir()
        self.write("PUBLICATION.toml", '''[publication]
schema = 1
version_file = "pyproject.toml"
version_key = "project.version"
changelog = "CHANGELOG.md"
evolution_dir = "docs/evolution"
source_roots = ["src", "scripts", "tests"]
public_url_hosts = ["github.com"]
''')
        self.set_version("1.0.0")
        self.write("src/demo.py", '# Entirely synthetic example.\nVALUE = "example-value"\n')
        self.write("CHANGELOG.md", self.notes("1.0.0"))
        self.write("docs/evolution/1.0.0.md", self.evolution())
        self.files = ["PUBLIC-FILES.json", "PUBLICATION.toml", "pyproject.toml", "CHANGELOG.md", "src/demo.py", "docs/evolution/1.0.0.md"]
        self.manifest()

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def manifest(self):
        self.write("PUBLIC-FILES.json", json.dumps({"schema": 1, "files": sorted(self.files)}, indent=2) + "\n")

    def set_version(self, version):
        self.write("pyproject.toml", '[project]\nname = "synthetic-demo"\nversion = "' + version + '"\n')

    @staticmethod
    def notes(version):
        return "# Changelog\n\n## [" + version + "] - 2026-10-07\n\nPublication boundary and entirely synthetic examples are verified.\n"

    @staticmethod
    def evolution():
        return """# Public research evolution

## Problem
Make a public architecture boundary explicit.

## Architecture and interface
Version the public implementation and document interface changes.

## Decisions and alternatives
Use an exact file allowlist because directory copying can include runtime data.

## Research hypotheses
The reuse benefit remains an unverified research hypothesis.

## Validation and limits
Run synthetic boundary tests; no actual records or runtime data are used.

## Next
Review future public changes with two independent reviewers.
"""

    def rejected(self, category, base_ref=None):
        with self.assertRaises(guard.AuditError) as caught:
            guard.check(self.root, base_ref=base_ref)
        self.assertIn(category, [issue.category for issue in caught.exception.issues])
        return caught.exception

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def baseline(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic publication test")
        self.git("config", "user.email", "publication@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic baseline")
        return self.git("rev-parse", "HEAD")

    def test_valid_tree_and_reserved_synthetic_uuid(self):
        self.write("src/demo.py", 'ID = "00000000-0000-0000-0000-000000000000"\n')
        result = guard.check(self.root)
        self.assertEqual(result.manifest["version"], "1.0.0")
        self.assertEqual(set(result.payload), set(self.files))

    def test_manifest_is_required_and_unlisted_file_fails(self):
        self.write("new-file.md", "Unreviewed public content.\n")
        self.rejected("unlisted-file")
        (self.root / "new-file.md").unlink()
        (self.root / "PUBLIC-FILES.json").unlink()
        self.rejected("manifest-missing")

    def test_actual_data_formats_fail_even_when_explicitly_listed(self):
        for extension in ("jsonl", "log", "db", "sqlite3", "safetensors"):
            with self.subTest(extension=extension):
                relative = "sample." + extension
                self.write(relative, "Entirely synthetic, still a forbidden publication format.\n")
                self.files.append(relative)
                self.manifest()
                self.rejected("forbidden-file-type")
                self.files.remove(relative)
                (self.root / relative).unlink()
                self.manifest()

    def test_symlink_and_nested_git_fail_without_reading_targets(self):
        (self.root / "sample.py").symlink_to(self.root / "nonexistent-private-target")
        self.rejected("symlink")
        (self.root / "sample.py").unlink()
        self.write("nested/.git/config", "Synthetic metadata.\n")
        self.rejected("nested-git")

    def test_arbitrary_json_data_is_not_a_schema_exception(self):
        self.write("sample.json", json.dumps({"text": "Anonymous raw record would still be prohibited."}))
        self.files.append("sample.json")
        self.manifest()
        self.rejected("forbidden-json-data")

    def test_generic_json_schema_fields_are_allowed(self):
        self.write("demo.schema.json", json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {"record_id": {"type": "string"}, "token": {"type": "string"}}}))
        self.files.append("demo.schema.json")
        self.manifest()
        self.assertEqual(guard.check(self.root).manifest["version"], "1.0.0")

    def test_generic_loopback_implementation_and_python_version_are_allowed(self):
        self.write("src/demo.py", 'URL = f"http://127.0.0.1:{server.server_address[1]}/health"\n')
        self.assertEqual(guard.check(self.root).manifest["version"], "1.0.0")
        self.write("src/demo.py", 'URL = "http://localhost:8765/api"\nIPV6 = "http://[::1]:8765/api"\nDYNAMIC = f"http://{server.server_address[0]}:{server.server_address[1]}/api"\nEXAMPLE = "https://api.example.com/oauth"\n')
        self.write(".python-version", "3.13\n")
        self.files.append(".python-version")
        self.manifest()
        self.assertEqual(guard.check(self.root).manifest["version"], "1.0.0")

    def test_symbolic_url_does_not_hide_credential_query_or_actual_record_id(self):
        self.write("src/demo.py", 'URL = "http://' + '{host}/{path}?' + 'access_' + 'to' + 'ken' + chr(61) + 'unapproved-value"\n')
        self.rejected("url-credential")
        self.write("src/demo.py", 'ID = "rec_' + ('a' * 32) + '"\n')
        self.rejected("record-identifier")

    def test_svg_logo_cannot_embed_active_or_external_content(self):
        self.write("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>Example</text></svg>')
        self.files.append("logo.svg")
        self.manifest()
        self.assertEqual(guard.check(self.root).manifest["version"], "1.0.0")
        self.write("logo.svg", '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
        self.rejected("active-or-external-svg")

    def test_exact_synthetic_config_schema_rejects_added_raw_record_fields(self):
        self.write("PUBLICATION.toml", (self.root / "PUBLICATION.toml").read_text() + '\n[publication.synthetic_config_files]\n"examples/profile.json" = "docs/profile.schema.json"\n')
        self.write("docs/profile.schema.json", json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "additionalProperties": False, "required": ["purpose", "enabled"], "properties": {"purpose": {"type": "string", "const": "synthetic research only"}, "enabled": {"type": "boolean", "const": False}}}))
        self.write("examples/profile.json", json.dumps({"purpose": "synthetic research only", "enabled": False}))
        self.files.extend(["docs/profile.schema.json", "examples/profile.json"])
        self.manifest()
        self.assertEqual(guard.check(self.root).manifest["version"], "1.0.0")
        self.write("examples/profile.json", json.dumps({"purpose": "synthetic research only", "enabled": False, "body": "An anonymized real record must never become a config exception."}))
        self.rejected("synthetic-config-schema-mismatch")
        self.write("examples/profile.json", json.dumps({"purpose": "synthetic research only", "enabled": True}))
        self.rejected("synthetic-config-schema-mismatch")

    def test_private_path_uuid_and_url_are_rejected_without_values_in_report(self):
        samples = [
            ("personal-absolute-path", "/" + "Users" + "/" + "unapproved-person" + "/cache.txt"),
            ("record-identifier", "d724a982" + "-cc22-" + "4239-9b51-" + "1905ed9fecab"),
            ("private-host-url", "http://" + "10.14.19.11" + "/runtime"),
        ]
        for category, value in samples:
            with self.subTest(category=category):
                self.write("src/demo.py", "VALUE = " + repr(value) + "\n")
                error = self.rejected(category)
                self.assertNotIn(value, json.dumps(error.as_dict()))

    def test_synthetic_prefix_does_not_hide_credentials(self):
        value = "synthetic-" + "gh" + "p_" + ("A" * 40)
        self.write("src/demo.py", "TO" + "KEN = " + repr(value) + "\n")
        error = self.rejected("credential")
        self.assertNotIn(value, json.dumps(error.as_dict()))

    def test_source_change_without_version_increase_fails(self):
        base = self.baseline()
        self.write("src/demo.py", "VALUE = 2\n")
        self.rejected("version-not-advanced", base)

    def test_docs_maintenance_also_requires_version_increase(self):
        base = self.baseline()
        self.write("docs/evolution/1.0.0.md", self.evolution() + "\nA new public design decision.\n")
        self.rejected("version-not-advanced", base)

    def test_version_increase_requires_current_release_notes_and_evolution(self):
        base = self.baseline()
        self.write("src/demo.py", "VALUE = 2\n")
        self.set_version("1.0.1")
        self.rejected("release-notes-missing", base)

    def test_documented_version_increase_passes_git_baseline_check(self):
        base = self.baseline()
        self.write("src/demo.py", "VALUE = 2\n")
        self.set_version("1.0.1")
        self.write("CHANGELOG.md", self.notes("1.0.1") + "\n" + self.notes("1.0.0").split("\n\n", 1)[1])
        self.write("docs/evolution/1.0.1.md", self.evolution() + "\nThis version changes the synthetic demo.\n")
        self.files.append("docs/evolution/1.0.1.md")
        self.manifest()
        result = guard.check(self.root, base_ref=base)
        self.assertEqual(result.manifest["version"], "1.0.1")
        self.assertGreater(result.manifest["changed_public_files"], 0)

    def test_build_copies_only_audited_bytes_and_never_git_metadata(self):
        self.baseline()
        result = guard.check(self.root)
        output = Path(self.temp.name) / "release-payload"
        guard.build(result, output)
        self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}, set(self.files))
        self.assertFalse((output / ".git").exists())

    def test_release_extraction_selects_exact_current_version(self):
        self.write("CHANGELOG.md", self.notes("1.0.1") + "\n" + self.notes("1.0.0").split("\n\n", 1)[1])
        self.assertIn("[1.0.0]", guard.release_notes(guard.check(self.root)))
        self.assertNotIn("[1.0.1]", guard.release_notes(guard.check(self.root)))


if __name__ == "__main__":
    unittest.main()
