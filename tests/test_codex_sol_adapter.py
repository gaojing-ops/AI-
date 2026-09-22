# -*- coding: utf-8 -*-

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import codex_sol_adapter


MODEL = "gpt-5.6-sol"
TRUSTED_SHA256 = (
    "83751f15cb6a0a7b97df67752c001e3fe1c20e18ffbfec3ff63567296205eb6c"
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
OUTPUT = {"verdict": "PASS"}


def _jsonl(*, model=None, failed=False, malformed=False):
    events = [
        {"type": "thread.started", "thread_id": "thread-test-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": json.dumps(OUTPUT, ensure_ascii=False),
            },
        },
    ]
    if model is not None:
        events[0]["model"] = model
    if failed:
        events.append({"type": "turn.failed", "error": {"message": "no"}})
    else:
        events.append({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 2,
                "output_tokens": 4,
            },
        })
    rendered = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    return rendered + ("\nnot-json" if malformed else "")


class FakeRunner:
    def __init__(
        self,
        *,
        stdout=None,
        returncode=0,
        stderr="",
        error=None,
        output=None,
    ):
        self.stdout = _jsonl() if stdout is None else stdout
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.output = OUTPUT if output is None else output
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if self.error:
            raise self.error
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(
            json.dumps(self.output, ensure_ascii=False),
            encoding="utf-8",
        )
        return types.SimpleNamespace(
            stdout=self.stdout,
            stderr=self.stderr,
            returncode=self.returncode,
        )


class CodexSolAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.exe = self.root / "codex.exe"
        self.exe.write_bytes(b"test executable placeholder")

    def tearDown(self):
        self.temp.cleanup()

    def run_adapter(self, runner, **kwargs):
        return codex_sol_adapter.run_codex_sol(
            "请审查这一章",
            SCHEMA,
            executable=self.exe,
            runner=runner,
            temp_parent=self.root,
            **kwargs,
        )

    def run_untrusted_formal(self, **kwargs):
        return codex_sol_adapter.run_codex_sol(
            "请审查这一章",
            SCHEMA,
            executable=self.exe,
            temp_parent=self.root,
            formal=True,
            pinned_executable_sha256=hashlib.sha256(
                self.exe.read_bytes()
            ).hexdigest(),
            **kwargs,
        )

    def make_official_package_tree(
        self,
        label="official-tree",
        *,
        native_name="@openai/codex",
        native_version="0.145.0-win32-x64",
        root_name="@openai/codex",
        root_version="0.145.0",
        dependency_spec="npm:@openai/codex@0.145.0-win32-x64",
        native_directory="codex-win32-x64",
        executable_relative_path=(
            "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
        ),
    ):
        scope_root = self.root / label / "node_modules" / "@openai"
        native_root = scope_root / native_directory
        executable = native_root.joinpath(*executable_relative_path.split("/"))
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"test-only native executable")
        native_package_path = native_root / "package.json"
        native_package_path.write_text(
            json.dumps({"name": native_name, "version": native_version}),
            encoding="utf-8",
        )
        root_package_root = scope_root / "codex"
        root_package_root.mkdir(parents=True, exist_ok=True)
        root_package_path = root_package_root / "package.json"
        root_package_path.write_text(
            json.dumps({
                "name": root_name,
                "version": root_version,
                "optionalDependencies": {
                    "@openai/codex-win32-x64": dependency_spec,
                },
            }),
            encoding="utf-8",
        )
        return executable.resolve(), native_package_path.resolve(), root_package_path.resolve()

    def make_test_only_bundle(self):
        prompt_sha256 = hashlib.sha256(b"test-only-prompt").hexdigest()
        command = codex_sol_adapter.build_command(
            str(self.exe.resolve()),
            model=MODEL,
            schema_path=self.root / "test-only.schema.json",
            output_path=self.root / "test-only.output.json",
        )
        bundle = {
            "version": codex_sol_adapter.FORMAL_BUNDLE_VERSION,
            "adapter_version": codex_sol_adapter.ADAPTER_VERSION,
            "protocol_version": codex_sol_adapter.PROTOCOL_VERSION,
            "prompt_sha256": prompt_sha256,
            "output_schema": SCHEMA,
            "output_text": json.dumps(OUTPUT),
            "events": [json.loads(line) for line in _jsonl().splitlines()],
            "command": command,
            "receipt": {
                "executable_path": str(self.exe.resolve()),
                "target_model": MODEL,
                "requested_model": MODEL,
                "command_bound_model": MODEL,
                "formal_receipt_eligible": False,
                "fixture_scope": "test_only_never_formal",
            },
        }
        bundle["bundle_sha256"] = codex_sol_adapter._bundle_digest(bundle)
        return bundle, prompt_sha256

    def make_test_only_release_evidence(self):
        executable, native_package_path, root_package_path = (
            self.make_official_package_tree(label="evidence-tree")
        )
        exe_path = str(executable)
        probe_command = [exe_path, "--version"]
        return {
            "trust_root": "embedded_openai_codex_release_allowlist_v2",
            "evidence_scope": "local_operational_evidence_not_remote_attestation",
            "release_id": "@openai/codex@0.145.0/windows-x64",
            "platform": "windows",
            "architecture": "x64",
            "executable_path": exe_path,
            "executable_sha256": TRUSTED_SHA256,
            "windows_file_identity": {
                "volume_serial": "01234567",
                "file_id": "0123456789abcdef",
            },
            "native_package_alias": "@openai/codex-win32-x64",
            "native_package_directory": "codex-win32-x64",
            "native_package_json_path": str(native_package_path),
            "native_package_json_sha256": "1" * 64,
            "native_package_name": "@openai/codex",
            "native_package_version": "0.145.0-win32-x64",
            "native_executable_relative_path": (
                "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            ),
            "root_package_directory": "codex",
            "root_package_json_path": str(root_package_path),
            "root_package_json_sha256": "2" * 64,
            "root_package_name": "@openai/codex",
            "root_package_version": "0.145.0",
            "native_dependency_spec": (
                "npm:@openai/codex@0.145.0-win32-x64"
            ),
            "cli_version": "codex-cli 0.145.0",
            "version_probe": {
                "command": probe_command,
                "command_sha256": codex_sol_adapter._sha256_text(
                    codex_sol_adapter._canonical_json(probe_command)
                ),
                "returncode": 0,
                "stdout_normalized": "codex-cli 0.145.0",
                "stdout_sha256": hashlib.sha256(
                    b"codex-cli 0.145.0\n"
                ).hexdigest(),
                "stderr_sha256": EMPTY_SHA256,
                "stderr_empty": True,
            },
        }

    def test_missing_explicit_path_and_missing_path_lookup_fail_closed(self):
        with self.assertRaises(codex_sol_adapter.CodexSolExecutableError):
            codex_sol_adapter.resolve_executable(self.root / "missing.exe")
        with self.assertRaises(codex_sol_adapter.CodexSolExecutableError):
            codex_sol_adapter.resolve_executable(which=lambda _name: None)

    def test_which_resolution_is_explicit_and_checked(self):
        resolved = codex_sol_adapter.resolve_executable(
            which=lambda name: str(self.exe) if name == "codex" else None
        )
        self.assertEqual(str(self.exe.resolve()), resolved)

    def test_access_denied_is_a_scoped_error(self):
        runner = FakeRunner(error=PermissionError("sensitive operating detail"))
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolAccessError,
            "denied access",
        ) as raised:
            self.run_adapter(runner)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

        win_error = OSError("sensitive windows detail")
        win_error.winerror = 5
        with self.assertRaises(codex_sol_adapter.CodexSolAccessError):
            self.run_adapter(FakeRunner(error=win_error))

    def test_timeout_is_fail_closed(self):
        runner = FakeRunner(
            error=subprocess.TimeoutExpired([str(self.exe), "exec"], 1)
        )
        with self.assertRaises(codex_sol_adapter.CodexSolTimeoutError) as raised:
            self.run_adapter(runner, timeout_seconds=1)
        self.assertIsNone(raised.exception.__cause__)

    def test_malformed_jsonl_and_turn_failed_are_rejected(self):
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "malformed",
        ) as raised:
            self.run_adapter(FakeRunner(stdout=_jsonl(malformed=True)))
        self.assertIsNone(raised.exception.__cause__)
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "turn.failed",
        ):
            self.run_adapter(FakeRunner(stdout=_jsonl(failed=True)))

    def test_command_is_fixed_safe_and_prompt_uses_stdin(self):
        runner = FakeRunner()
        self.run_adapter(runner)
        command, kwargs = runner.calls[0]
        self.assertEqual(str(self.exe.resolve()), command[0])
        self.assertEqual("exec", command[1])
        self.assertEqual(MODEL, command[command.index("--model") + 1])
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertIn("--ephemeral", command)
        self.assertIn("--json", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--output-schema", command)
        self.assertIn("-o", command)
        self.assertEqual("-", command[-1])
        self.assertNotIn("请审查这一章", command)
        self.assertEqual("请审查这一章", kwargs["input"])
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertEqual("utf-8", kwargs["encoding"])
        self.assertTrue(kwargs["capture_output"])
        self.assertIn("env", kwargs)
        self.assertNotIn("auth", " ".join(command).lower())
        if os.name == "nt" and getattr(subprocess, "CREATE_NO_WINDOW", 0):
            self.assertEqual(subprocess.CREATE_NO_WINDOW, kwargs["creationflags"])

    def test_custom_runner_is_nonformal_even_with_reported_model(self):
        result = self.run_adapter(FakeRunner(stdout=_jsonl(model=MODEL)))
        schema_text = json.dumps(
            SCHEMA,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        output_text = json.dumps(OUTPUT, ensure_ascii=False)
        self.assertEqual(MODEL, result["target_model"])
        self.assertEqual(MODEL, result["requested_model"])
        self.assertIsNone(result["command_bound_model"])
        self.assertEqual(MODEL, result["reported_model"])
        self.assertEqual(
            "custom_runner_nonformal", result["model_provenance"]["status"]
        )
        self.assertEqual(
            "trusted_cli_event",
            result["model_provenance"]["reported_model_source"],
        )
        self.assertFalse(result["formal_receipt_eligible"])
        self.assertEqual(
            hashlib.sha256("请审查这一章".encode("utf-8")).hexdigest(),
            result["prompt_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
            result["output_schema_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
            result["response_sha256"],
        )
        self.assertIsNone(result["formal_receipt"])
        self.assertIsNone(result["formal_bundle"])
        self.assertEqual(codex_sol_adapter.ADAPTER_VERSION, result["adapter_version"])

    def test_formal_rejects_self_pinned_unknown_release_before_launch(self):
        with mock.patch("codex_sol_adapter.subprocess.run") as production_run:
            with self.assertRaisesRegex(
                codex_sol_adapter.CodexSolProtocolError,
                "embedded official release allowlist",
            ):
                self.run_untrusted_formal()
        production_run.assert_not_called()

    def test_caller_pin_cannot_expand_internal_release_allowlist(self):
        with mock.patch("codex_sol_adapter.subprocess.run") as production_run:
            with self.assertRaisesRegex(
                codex_sol_adapter.CodexSolProtocolError,
                "pinned official release",
            ):
                codex_sol_adapter.run_codex_sol(
                    "prompt",
                    SCHEMA,
                    executable=self.exe,
                    temp_parent=self.root,
                    formal=True,
                    pinned_executable_sha256=TRUSTED_SHA256,
                )
        production_run.assert_not_called()

    def test_formal_rejects_custom_runner_wrong_pin_and_wrong_model(self):
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "custom runner",
        ):
            codex_sol_adapter.run_codex_sol(
                "prompt",
                SCHEMA,
                executable=self.exe,
                runner=FakeRunner(),
                temp_parent=self.root,
                formal=True,
                pinned_executable_sha256=TRUSTED_SHA256,
            )
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "embedded official release allowlist",
        ):
            codex_sol_adapter.run_codex_sol(
                "prompt",
                SCHEMA,
                executable=self.exe,
                temp_parent=self.root,
                formal=True,
                pinned_executable_sha256="0" * 64,
            )
        for wrong_model in ("gpt-6-sol", " gpt-5.6-sol "):
            with self.subTest(wrong_model=wrong_model):
                with self.assertRaisesRegex(
                    codex_sol_adapter.CodexSolProtocolError,
                    "requires exact model gpt-5.6-sol",
                ):
                    codex_sol_adapter.run_codex_sol(
                        "prompt",
                        SCHEMA,
                        executable=self.exe,
                        temp_parent=self.root,
                        formal=True,
                        model=wrong_model,
                        pinned_executable_sha256=TRUSTED_SHA256,
                    )

    def test_embedded_release_and_two_layer_package_identity(self):
        release = codex_sol_adapter._trusted_release_for_hash(TRUSTED_SHA256)
        self.assertEqual("@openai/codex", release["root_package_name"])
        self.assertEqual("0.145.0", release["root_package_version"])
        self.assertEqual("@openai/codex", release["native_package_name"])
        self.assertEqual(
            "0.145.0-win32-x64", release["native_package_version"]
        )
        self.assertEqual(
            "vendor/x86_64-pc-windows-msvc/bin/codex.exe",
            release["native_executable_relative_path"],
        )
        self.assertEqual("codex-cli 0.145.0", release["cli_version"])
        self.assertEqual("codex-cli 0.145.0\n", release["cli_version_stdout"])
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "embedded official release allowlist",
        ):
            codex_sol_adapter._trusted_release_for_hash("0" * 64)

        executable, native_package, root_package = self.make_official_package_tree()
        identity = codex_sol_adapter._load_adjacent_package_identity(
            str(executable), release
        )
        self.assertEqual(str(native_package), identity["native_package_json_path"])
        self.assertEqual(str(root_package), identity["root_package_json_path"])
        self.assertEqual("@openai/codex", identity["native_package_name"])
        self.assertEqual(
            "0.145.0-win32-x64", identity["native_package_version"]
        )
        self.assertEqual("@openai/codex", identity["root_package_name"])
        self.assertEqual("0.145.0", identity["root_package_version"])

    def test_two_layer_package_identity_rejects_versions_dependency_and_paths(self):
        release = codex_sol_adapter._trusted_release_for_hash(TRUSTED_SHA256)
        cases = (
            ({"native_version": "0.145.1-win32-x64"}, "native package"),
            ({"root_version": "0.145.1"}, "root package"),
            (
                {"dependency_spec": "npm:@openai/codex@0.145.1-win32-x64"},
                "does not bind",
            ),
            ({"native_directory": "codex-win32-x64-copy"}, "native package path"),
            (
                {
                    "executable_relative_path": (
                        "vendor/x86_64-pc-windows-msvc/other/codex.exe"
                    )
                },
                "native vendor path",
            ),
        )
        for index, (overrides, message) in enumerate(cases):
            with self.subTest(overrides=overrides):
                executable, _native, _root = self.make_official_package_tree(
                    label=f"invalid-tree-{index}",
                    **overrides,
                )
                with self.assertRaisesRegex(
                    codex_sol_adapter.CodexSolProtocolError,
                    message,
                ):
                    codex_sol_adapter._load_adjacent_package_identity(
                        str(executable), release
                    )

    def test_version_probe_is_strict_but_main_stderr_can_be_recorded(self):
        clean = types.SimpleNamespace(
            returncode=0,
            stdout="codex-cli 0.145.0\n",
            stderr="",
        )
        evidence = codex_sol_adapter._validate_clean_process_result(
            clean,
            stage="probe",
            expected_stdout="codex-cli 0.145.0\n",
        )
        self.assertTrue(evidence["stderr_empty"])
        self.assertEqual(EMPTY_SHA256, evidence["stderr_sha256"])

        cases = [
            types.SimpleNamespace(returncode=0, stdout="codex-cli 0.145.0", stderr=""),
            types.SimpleNamespace(returncode=0, stdout="codex-cli 0.144.0\n", stderr=""),
            types.SimpleNamespace(returncode=0, stdout="codex-cli 0.145.1\n", stderr=""),
            types.SimpleNamespace(
                returncode=0,
                stdout="codex-cli 0.145.0\nextra\n",
                stderr="",
            ),
            types.SimpleNamespace(
                returncode=0,
                stdout="codex-cli 0.145.0\n",
                stderr="warning",
            ),
            types.SimpleNamespace(
                returncode=7,
                stdout="codex-cli 0.145.0\n",
                stderr="",
            ),
        ]
        for completed in cases:
            with self.subTest(completed=completed):
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
                    codex_sol_adapter._validate_clean_process_result(
                        completed,
                        stage="probe",
                        expected_stdout="codex-cli 0.145.0\n",
                    )

        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "non-empty stderr",
        ):
            codex_sol_adapter._validate_clean_process_result(
                types.SimpleNamespace(
                    returncode=0,
                    stdout=_jsonl(),
                    stderr="success warning",
                ),
                stage="Codex exec",
            )

        main = codex_sol_adapter._validate_clean_process_result(
            types.SimpleNamespace(
                returncode=0,
                stdout=_jsonl(),
                stderr="success warning",
            ),
            stage="Codex exec",
            allow_stderr=True,
        )
        self.assertFalse(main["stderr_empty"])
        self.assertEqual(
            hashlib.sha256(b"success warning").hexdigest(),
            main["stderr_sha256"],
        )

    def test_formal_receipt_records_nonempty_success_stderr(self):
        diagnostic_sha256 = hashlib.sha256(
            b"transient diagnostic after successful retry"
        ).hexdigest()
        provenance = codex_sol_adapter._formal_model_provenance(None)
        result = {
            "thread_id": "thread-stderr-test",
            "target_model": MODEL,
            "requested_model": MODEL,
            "command_bound_model": MODEL,
            "reported_model": None,
            "provenance_basis": provenance["status"],
            "model_provenance": provenance,
            "prompt_sha256": "1" * 64,
            "output_schema_sha256": "2" * 64,
            "response_sha256": "3" * 64,
            "event_stream_sha256": "4" * 64,
            "evidence_scope": codex_sol_adapter.LOCAL_EVIDENCE_SCOPE,
            "executable_path": str(self.exe.resolve()),
            "executable_sha256": TRUSTED_SHA256,
            "executable_sha256_before": TRUSTED_SHA256,
            "executable_sha256_after": TRUSTED_SHA256,
            "command_policy_sha256": "5" * 64,
            "official_release": {"test": True},
            "process_returncode": 0,
            "success_stderr_sha256": diagnostic_sha256,
            "success_stderr_empty": False,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "adapter_version": codex_sol_adapter.ADAPTER_VERSION,
            "protocol_version": codex_sol_adapter.PROTOCOL_VERSION,
        }
        with mock.patch(
            "codex_sol_adapter._validate_official_release_evidence",
            return_value={"test": True},
        ):
            receipt = codex_sol_adapter._build_formal_receipt(result)
        self.assertFalse(receipt["success_stderr_empty"])
        self.assertEqual(
            diagnostic_sha256,
            receipt["success_stderr_sha256"],
        )
        self.assertFalse(
            receipt["execution_policy"]["success_stderr_required_empty"]
        )

    def test_formal_lock_failure_prevents_probe_and_main(self):
        with (
            mock.patch(
                "codex_sol_adapter._open_windows_executable_lock",
                side_effect=OSError("sharing violation"),
            ),
            mock.patch("codex_sol_adapter.subprocess.run") as production_run,
        ):
            with self.assertRaisesRegex(
                codex_sol_adapter.CodexSolAccessError,
                "integrity lock",
            ):
                codex_sol_adapter.run_codex_sol(
                    "prompt",
                    SCHEMA,
                    executable=self.exe,
                    temp_parent=self.root,
                    formal=True,
                    pinned_executable_sha256=TRUSTED_SHA256,
                )
        production_run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows locking contract")
    def test_windows_lock_blocks_write_delete_and_swap(self):
        replacement = self.root / "replacement.exe"
        replacement.write_bytes(b"replacement")
        with codex_sol_adapter._hold_formal_executable_lock(
            str(self.exe.resolve())
        ):
            for operation in (
                lambda: self.exe.write_bytes(b"changed"),
                lambda: self.exe.unlink(),
                lambda: os.replace(replacement, self.exe),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(OSError):
                        operation()
        self.assertEqual(b"test executable placeholder", self.exe.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows locking contract")
    def test_windows_integrity_lock_still_allows_execution(self):
        executable = str(Path(sys.executable).resolve())
        with codex_sol_adapter._hold_formal_executable_lock(executable):
            completed = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(0, completed.returncode)
        self.assertTrue(completed.stdout.strip())

    def test_fake_runner_and_test_only_bundle_can_never_be_formal(self):
        result = self.run_adapter(FakeRunner(stdout=_jsonl(model=None)))
        self.assertFalse(result["formal_receipt_eligible"])
        self.assertIsNone(result["formal_receipt"])
        self.assertEqual("nonformal_execution", result["evidence_scope"])

        bundle, prompt_sha256 = self.make_test_only_bundle()
        with mock.patch("codex_sol_adapter.subprocess.run") as production_run:
            with self.assertRaisesRegex(
                codex_sol_adapter.CodexSolProtocolError,
                "pinned official release",
            ):
                codex_sol_adapter.verify_formal_bundle(
                    bundle,
                    pinned_executable_sha256=TRUSTED_SHA256,
                    expected_prompt_sha256=prompt_sha256,
                )
        production_run.assert_not_called()

    def test_bundle_command_zero_path_rewrite_is_rejected(self):
        bundle, prompt_sha256 = self.make_test_only_bundle()
        bundle["command"][0] = str((self.root / "rewritten.exe").resolve())
        bundle["bundle_sha256"] = codex_sol_adapter._bundle_digest(bundle)
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "executable path binding mismatch",
        ):
            codex_sol_adapter.verify_formal_bundle(
                bundle,
                pinned_executable_sha256=TRUSTED_SHA256,
                expected_prompt_sha256=prompt_sha256,
            )

    def test_release_evidence_rejects_package_version_and_probe_tamper(self):
        original = self.make_test_only_release_evidence()
        executable_path = original["executable_path"]
        validated = codex_sol_adapter._validate_official_release_evidence(
            original,
            executable_path=executable_path,
            executable_sha256=TRUSTED_SHA256,
        )
        self.assertEqual("0.145.0", validated["root_package_version"])
        self.assertEqual(
            "0.145.0-win32-x64", validated["native_package_version"]
        )

        edits = []
        for path, value in (
            (("native_package_name",), "fake"),
            (("native_package_version",), "0.145.1-win32-x64"),
            (("root_package_name",), "fake"),
            (("root_package_version",), "0.145.1"),
            (("native_dependency_spec",), "npm:@openai/codex@0.145.1-win32-x64"),
            (("native_package_json_path",), original["root_package_json_path"]),
            (("root_package_json_path",), original["native_package_json_path"]),
            (("cli_version",), "codex-cli 0.145.1"),
            (("version_probe", "stdout_normalized"), "codex-cli 0.145.1"),
            (("version_probe", "stderr_empty"), False),
            (("version_probe", "stderr_sha256"), "f" * 64),
        ):
            changed = json.loads(json.dumps(original))
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            edits.append(changed)
        for changed in edits:
            with self.subTest(changed=changed):
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
                    codex_sol_adapter._validate_official_release_evidence(
                        changed,
                        executable_path=executable_path,
                        executable_sha256=TRUSTED_SHA256,
                    )

    def test_real_cli_event_shape_keeps_missing_model_command_bound(self):
        events = [json.loads(line) for line in _jsonl(model=None).splitlines()]
        evidence = codex_sol_adapter._validate_events(events, target_model=MODEL)
        provenance = codex_sol_adapter._formal_model_provenance(
            evidence["reported_model"]
        )
        self.assertIsNone(evidence["reported_model"])
        self.assertEqual("command_bound_official_cli", provenance["status"])
        self.assertIsNone(provenance["reported_model"])
        self.assertIsNone(provenance["reported_model_source"])

    def test_future_trusted_model_event_must_match_command(self):
        matching = [
            json.loads(line) for line in _jsonl(model=MODEL).splitlines()
        ]
        evidence = codex_sol_adapter._validate_events(
            matching,
            target_model=MODEL,
        )
        provenance = codex_sol_adapter._formal_model_provenance(
            evidence["reported_model"]
        )
        self.assertEqual(MODEL, evidence["reported_model"])
        self.assertEqual("event_reported_official_cli", provenance["status"])
        mismatching = [
            json.loads(line) for line in _jsonl(model="gpt-5.5").splitlines()
        ]
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "different model",
        ):
            codex_sol_adapter._validate_events(mismatching, target_model=MODEL)

    def test_real_event_shape_requires_thread_usage_and_item_message(self):
        cases = []
        missing_thread = [json.loads(line) for line in _jsonl().splitlines()]
        del missing_thread[0]["thread_id"]
        cases.append(missing_thread)
        missing_usage = [json.loads(line) for line in _jsonl().splitlines()]
        del missing_usage[-1]["usage"]
        cases.append(missing_usage)
        wrong_message = [json.loads(line) for line in _jsonl().splitlines()]
        wrong_message[2] = {
            "type": "agent_message",
            "text": json.dumps(OUTPUT),
        }
        cases.append(wrong_message)
        for events in cases:
            with self.subTest(events=events):
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
                    codex_sol_adapter._validate_events(events, target_model=MODEL)

    def test_missing_reported_model_with_custom_runner_stays_nonformal(self):
        result = self.run_adapter(FakeRunner(stdout=_jsonl(model=None)))
        self.assertEqual(
            "custom_runner_nonformal", result["model_provenance"]["status"]
        )
        self.assertIsNone(result["reported_model"])
        self.assertFalse(result["formal_receipt_eligible"])
        self.assertIsNone(result["formal_receipt"])
        self.assertIsNone(result["formal_bundle"])

    def test_model_mismatch_and_conflicting_provenance_are_rejected(self):
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "different model",
        ):
            self.run_adapter(FakeRunner(stdout=_jsonl(model="gpt-5.5")))

        lines = _jsonl(model=MODEL).splitlines()
        completed = json.loads(lines[-1])
        completed["model"] = "another-model"
        lines[-1] = json.dumps(completed)
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "contradictory",
        ):
            self.run_adapter(FakeRunner(stdout="\n".join(lines)))

    def test_unknown_event_top_level_model_is_not_provenance(self):
        events = [json.loads(line) for line in _jsonl(model=None).splitlines()]
        events[2]["model"] = MODEL
        result = self.run_adapter(
            FakeRunner(stdout="\n".join(json.dumps(event) for event in events))
        )
        self.assertEqual(
            "custom_runner_nonformal", result["model_provenance"]["status"]
        )
        self.assertIsNone(result["reported_model"])
        self.assertFalse(result["formal_receipt_eligible"])

    def test_explicit_future_model_is_not_silently_rewritten(self):
        future = "gpt-6-sol"
        runner = FakeRunner(stdout=_jsonl(model=future))
        result = self.run_adapter(runner, model=future)
        command, _kwargs = runner.calls[0]
        self.assertEqual(future, command[command.index("--model") + 1])
        self.assertEqual(future, result["reported_model"])

    def test_missing_lifecycle_message_usage_and_output_mismatch_fail(self):
        bad_sets = [
            [{"type": "turn.completed", "usage": {"input_tokens": 1}}],
            [
                {"type": "thread.started", "thread_id": "t", "model": MODEL},
                {"type": "turn.completed", "usage": {"input_tokens": 1}},
            ],
            [
                {"type": "thread.started", "thread_id": "t", "model": MODEL},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(OUTPUT)},
                },
                {"type": "turn.completed", "usage": {}},
            ],
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(OUTPUT)},
                },
                {"type": "thread.started", "thread_id": "t", "model": MODEL},
                {"type": "turn.completed", "usage": {"input_tokens": 1}},
            ],
        ]
        for events in bad_sets:
            with self.subTest(events=events):
                stdout = "\n".join(json.dumps(event) for event in events)
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
                    self.run_adapter(FakeRunner(stdout=stdout))

        class MismatchRunner(FakeRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                Path(command[command.index("-o") + 1]).write_text(
                    json.dumps({"verdict": "FAIL"}), encoding="utf-8"
                )
                return result

        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "does not match",
        ):
            self.run_adapter(MismatchRunner())

    def test_local_schema_validation_rejects_invalid_cli_output(self):
        invalid = {"verdict": 7, "extra": True}
        events = [json.loads(line) for line in _jsonl().splitlines()]
        events[2]["item"]["text"] = json.dumps(invalid, ensure_ascii=False)
        stdout = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "local schema validation",
        ):
            self.run_adapter(FakeRunner(stdout=stdout, output=invalid))

    def test_schema_subset_validates_nested_arrays_bounds_and_combinators(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 2, "maxLength": 5},
                "scores": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {"type": "integer"},
                },
                "choice": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "boolean", "const": True},
                    ]
                },
                "rating": {
                    "anyOf": [
                        {"type": "number", "enum": [1.5]},
                        {"type": "integer", "const": 2},
                    ]
                },
            },
            "required": ["name", "scores", "choice", "rating"],
            "additionalProperties": False,
        }
        codex_sol_adapter.validate_structured_output(
            schema,
            {"name": "林舟", "scores": [8, 9], "choice": True, "rating": 2},
        )
        with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
            codex_sol_adapter.validate_structured_output(
                schema,
                {"name": "林", "scores": [8, "9"], "choice": False, "rating": 3},
            )

    def test_unsupported_schema_keyword_fails_before_runner(self):
        runner = FakeRunner()
        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "Unsupported output schema keyword",
        ):
            codex_sol_adapter.run_codex_sol(
                "prompt",
                {"type": "string", "pattern": "unsafe unsupported keyword"},
                executable=self.exe,
                runner=runner,
                temp_parent=self.root,
            )
        self.assertEqual([], runner.calls)

    def test_subprocess_environment_removes_secrets_but_keeps_runtime_paths(self):
        source = {
            "PATH": "runtime-path",
            "SystemRoot": "runtime-root",
            "USERPROFILE": "user-home",
            "CODEX_HOME": "codex-home",
            "OPENAI_API_KEY": "do-not-pass",
            "DEEPSEEK_TOKEN": "do-not-pass",
            "CODEX_SECRET": "do-not-pass",
            "DATABASE_PASSWORD": "do-not-pass",
            "UNRELATED": "do-not-pass-either",
        }
        cleaned = codex_sol_adapter.build_subprocess_env(source)
        self.assertEqual("runtime-path", cleaned["PATH"])
        self.assertEqual("runtime-root", cleaned["SystemRoot"])
        self.assertEqual("user-home", cleaned["USERPROFILE"])
        self.assertEqual("codex-home", cleaned["CODEX_HOME"])
        self.assertNotIn("OPENAI_API_KEY", cleaned)
        self.assertNotIn("DEEPSEEK_TOKEN", cleaned)
        self.assertNotIn("CODEX_SECRET", cleaned)
        self.assertNotIn("DATABASE_PASSWORD", cleaned)
        self.assertNotIn("UNRELATED", cleaned)

        runner = FakeRunner()
        with mock.patch.dict(
            "os.environ",
            {
                "OPENAI_KEY": "do-not-pass",
                "CODEX_TOKEN": "do-not-pass",
                "DEEPSEEK_APIKEY": "do-not-pass",
            },
            clear=False,
        ):
            self.run_adapter(runner)
        child_env = runner.calls[0][1]["env"]
        self.assertNotIn("OPENAI_KEY", child_env)
        self.assertNotIn("CODEX_TOKEN", child_env)
        self.assertNotIn("DEEPSEEK_APIKEY", child_env)

    def test_nonstandard_json_duplicate_keys_and_json_type_equality_fail(self):
        codex_sol_adapter.validate_structured_output(
            {"enum": [1]},
            1,
        )
        with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
            codex_sol_adapter.validate_structured_output({"enum": [1]}, True)

        events = [json.loads(line) for line in _jsonl().splitlines()]
        events[2]["item"]["text"] = '{"verdict":"PASS","verdict":"FAIL"}'
        stdout = "\n".join(json.dumps(event) for event in events)

        class DuplicateOutputRunner(FakeRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                Path(command[command.index("-o") + 1]).write_text(
                    '{"verdict":"PASS","verdict":"FAIL"}',
                    encoding="utf-8",
                )
                return result

        with self.assertRaisesRegex(
            codex_sol_adapter.CodexSolProtocolError,
            "not valid JSON",
        ):
            self.run_adapter(DuplicateOutputRunner(stdout=stdout))

    def test_recovered_error_event_is_recorded_but_lifecycle_remains_strict(self):
        normal = [json.loads(line) for line in _jsonl().splitlines()]
        cases = []
        cases.append([event for event in normal if event["type"] != "turn.started"])
        duplicate = list(normal)
        duplicate.insert(2, {"type": "turn.started"})
        cases.append(duplicate)
        for missing_field in ("input_tokens", "output_tokens"):
            missing_usage = json.loads(json.dumps(normal))
            del missing_usage[-1]["usage"][missing_field]
            cases.append(missing_usage)
        negative_usage = json.loads(json.dumps(normal))
        negative_usage[-1]["usage"]["input_tokens"] = -1
        cases.append(negative_usage)

        for events in cases:
            with self.subTest(events=events):
                stdout = "\n".join(json.dumps(event) for event in events)
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError):
                    self.run_adapter(FakeRunner(stdout=stdout))

        error_events = list(normal[:-1]) + [
            {"type": "error", "message": "transient retry"},
            normal[-1],
        ]
        stdout = "\n".join(json.dumps(event) for event in error_events)
        recovered = self.run_adapter(FakeRunner(stdout=stdout))
        self.assertEqual(1, recovered["diagnostic_error_event_count"])

    def test_nonzero_exit_reports_safe_failure_hint_and_stdout_hash(self):
        cases = [
            ({"code": "usage_limit_reached"}, "usage_limit"),
            ({"message": "You've hit your usage limit. SECRET"}, "usage_limit"),
            ({"code": "rate_limit_exceeded"}, "rate_limit"),
            ({"message": "Selected model is at capacity. Please try a different model."}, "service_capacity"),
            ({"message": "stream disconnected before completion: SECRET"}, "connection"),
            ({"code": "invalid_api_key"}, "authentication"),
            ({"code": "context_length_exceeded"}, "context_limit"),
            ({"message": "SECRET"}, "unknown"),
        ]
        for error, hint in cases:
            with self.subTest(hint=hint, error=error):
                stdout = json.dumps({"type": "turn.failed", "error": error})
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError) as raised:
                    self.run_adapter(FakeRunner(returncode=1, stdout=stdout, stderr="SECRET"))
                message = str(raised.exception)
                self.assertIn("failure_hint=" + hint, message)
                self.assertIn("stdout_sha256=" + hashlib.sha256(stdout.encode()).hexdigest(), message)
                self.assertNotIn("SECRET", message)

    def test_failure_hint_uses_last_error_not_agent_text_or_old_retry(self):
        stdout = "\n".join(json.dumps(event) for event in [
            {"type": "error", "message": "stream disconnected before completion"},
            {"type": "item.completed", "item": {"text": "usage_limit_reached"}},
            {"type": "turn.failed", "error": {"message": "unrecognized failure"}},
        ])
        with self.assertRaises(codex_sol_adapter.CodexSolProtocolError) as raised:
            self.run_adapter(FakeRunner(returncode=1, stdout=stdout))
        self.assertIn("failure_hint=unknown", str(raised.exception))

    def test_failure_hint_handles_malformed_and_unexpected_payloads(self):
        for stdout in ["bad JSON", "[]", '{"type":"turn.failed","error":[]}',
                       '{"type":"error","message":{"secret":"usage_limit_reached"}}']:
            with self.subTest(stdout=stdout):
                with self.assertRaises(codex_sol_adapter.CodexSolProtocolError) as raised:
                    self.run_adapter(FakeRunner(returncode=1, stdout=stdout))
                self.assertIn("failure_hint=unknown", str(raised.exception))

    def test_nonzero_exit_only_exposes_stderr_hash(self):
        secret = "SECRET_SHOULD_NOT_BE_ECHOED"
        runner = FakeRunner(returncode=7, stderr=secret)
        with self.assertRaises(codex_sol_adapter.CodexSolProtocolError) as raised:
            self.run_adapter(runner)
        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertIn(hashlib.sha256(secret.encode()).hexdigest(), message)


if __name__ == "__main__":
    unittest.main()
