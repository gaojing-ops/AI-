# -*- coding: utf-8 -*-
import os
import json
import hashlib
import tempfile
import types
import unittest
from unittest import mock

import generator
import book_initializer
import commercial_reviewer
import continuity_guard
import narrative_guard
import story_architect
import gui_app
from gui_app import NovelGeneratorGUI


def write_semantic_invariants(plot_dir):
    os.makedirs(plot_dir, exist_ok=True)
    with open(
        os.path.join(plot_dir, "semantic_invariants.json"),
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump({
            "identity_bindings": [{
                "id": "test_identity",
                "entity": "林舟身份",
                "claim_patterns": ["林舟身份[：:](?P<value>主角|反派)"],
                "allowed_values": ["主角"],
            }],
            "exclusive_fact_groups": [],
            "forbidden_patterns": [],
            "numeric_rules": [],
        }, handle, ensure_ascii=False, indent=2)


def valid_automatic_audit(text, chapter=1):
    model = "deepseek-v4-pro"
    payload = {
        "verdict": "PASS",
        "scores": {
            "coherence": 90,
            "outline_fulfillment": 90,
            "mainline_alignment": 90,
            "character_consistency": 90,
            "prose_validity": 90,
            "commercial_progress": 90,
        },
        "outline_requirements": [],
        "outline_coverage": [],
        "mainline_progress": "林舟进入旧站调查",
        "progress_evidence_quote": "林舟推开旧站铁门",
        "drift_flags": [],
        "nonsense_flags": [],
        "fail_reasons": [],
        "summary": "通过",
    }
    raw_response = json.dumps(payload, ensure_ascii=False)
    normalized, issues = narrative_guard.validate_audit(
        payload,
        chapter_text=text,
        requirements=[],
    )
    if issues:
        raise AssertionError(issues)
    normalized.update({
        "chapter": chapter,
        "chapter_sha256": narrative_guard.chapter_sha256(text),
        "review_model": model,
        "review_raw_response": raw_response,
    })
    normalized["review_receipt"] = narrative_guard.build_review_receipt(
        chapter_text=text,
        system_prompt="自动叙事审查",
        user_prompt=f"审查第{chapter}章",
        raw_response=raw_response,
        review_model=model,
        decision_payload=normalized,
    )
    return normalized


class OneClickGuiTests(unittest.TestCase):
    def test_corrupt_revision_debt_blocks_health_context_and_action_writes(self):
        app = self.make_app(revision_debt_enabled=True)
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root}, clear=True
        ):
            debt_path = os.path.join(root, "revision_debt.json")
            plan_path = os.path.join(root, "商业审稿行动单.md")
            with open(debt_path, "w", encoding="utf-8") as handle:
                handle.write("{truncated")
            with open(plan_path, "w", encoding="utf-8") as handle:
                handle.write("旧行动单")
            message, status = app._revision_debt_health_status(70)
            self.assertEqual(status, "FAIL")
            self.assertIn("修订债务", message)
            with self.assertRaises(commercial_reviewer.RevisionDebtError):
                app._commercial_contract_context()
            with self.assertRaises(commercial_reviewer.RevisionDebtError):
                app._persist_commercial_review_actions(
                    {"review_id": "r69", "next_actions": ["新任务"]}, 69
                )
            with open(debt_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "{truncated")
            with open(plan_path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "旧行动单")

    def make_app(self, **config):
        app = NovelGeneratorGUI.__new__(NovelGeneratorGUI)
        app.config = config
        app._model_call_count = 0
        app._model_call_limit = 0
        app._chapter_model_call_count = 0
        app._chapter_model_call_limit = 0
        app._chapter_model_call_number = 0
        app._model_usage_persistence_fault = ""
        app._model_usage_unpersisted_cost_cny = 0.0
        import threading
        app._model_call_lock = threading.Lock()
        app._ui = lambda callback: None
        return app

    def test_placeholder_is_not_a_runtime_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                generator.get_runtime_api_key(
                    {"api_key": "YOUR_DEEPSEEK_API_KEY_HERE"}
                ),
                "",
            )

    def test_legacy_evidence_boundary_exempts_only_sealed_prefix(self):
        app = self.make_app(legacy_evidence_exempt_through_chapter=30)

        self.assertEqual(30, app._legacy_evidence_exempt_through_chapter())
        self.assertFalse(app._chapter_requires_current_evidence(1))
        self.assertFalse(app._chapter_requires_current_evidence(30))
        self.assertTrue(app._chapter_requires_current_evidence(31))
        self.assertTrue(app._chapter_requires_current_evidence(63))

    def test_invalid_legacy_evidence_boundary_fails_closed(self):
        app = self.make_app(legacy_evidence_exempt_through_chapter="invalid")

        self.assertEqual(0, app._legacy_evidence_exempt_through_chapter())
        self.assertTrue(app._chapter_requires_current_evidence(1))

    def test_project_config_loads_legacy_evidence_boundary_from_disk(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as project:
            path = os.path.join(project, "project_config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "model_provider": "deepseek",
                    "legacy_evidence_exempt_through_chapter": 30,
                    "doctor_evidence_enforcement": True,
                    "trial_continue_confirmed": True,
                }, handle)

            app._load_project_config(project)

        self.assertEqual(30, app._legacy_evidence_exempt_through_chapter())
        self.assertTrue(app.config["doctor_evidence_enforcement"])
        self.assertFalse(app._chapter_requires_current_evidence(30))
        self.assertTrue(app._chapter_requires_current_evidence(31))

    def test_historical_gate_audits_materialized_ledger_at_live_tip(self):
        app = self.make_app()
        app._official_chapter_paths_by_number = mock.Mock(
            return_value={1: "chapter-1", 62: "chapter-62"}
        )

        self.assertEqual(62, app._evidence_ledger_audit_chapter(3))
        self.assertEqual(62, app._evidence_ledger_audit_chapter(60))

    def test_revision_debt_blocks_only_after_due_chapter(self):
        debt = {
            "items": [
                {"id": "future", "status": "OPEN", "due_by": 70},
                {"id": "overdue", "status": "OPEN", "due_by": 62},
                {"id": "closed", "status": "CLOSED", "due_by": 1},
            ]
        }

        self.assertEqual([], NovelGeneratorGUI._overdue_revision_debts(debt, 62))
        self.assertEqual(
            ["overdue"],
            [
                item["id"]
                for item in NovelGeneratorGUI._overdue_revision_debts(debt, 63)
            ],
        )

    def test_project_provider_missing_migrates_to_deepseek(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as project:
            path = os.path.join(project, "project_config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"book_title": "旧书"}, handle, ensure_ascii=False)
            app._load_project_config(project)
            with open(path, "r", encoding="utf-8") as handle:
                persisted = json.load(handle)
        self.assertEqual("deepseek", persisted["model_provider"])
        self.assertEqual("deepseek", app.config["model_provider"])
        self.assertEqual("", app._project_config_error)

    def test_unknown_project_provider_fails_closed_without_rewriting_value(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as project:
            path = os.path.join(project, "project_config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"model_provider": "mystery"}, handle)
            app._load_project_config(project)
            with open(path, "r", encoding="utf-8") as handle:
                persisted = json.load(handle)
        self.assertEqual("mystery", persisted["model_provider"])
        self.assertEqual("mystery", app.config["model_provider"])
        self.assertIn("未知模型提供方", app._project_config_error)

    def test_codex_local_readiness_does_not_require_deepseek_key(self):
        app = self.make_app(
            model_provider=gui_app.PROVIDER_CODEX_SOL,
            api_key="YOUR_DEEPSEEK_API_KEY_HERE",
        )
        with tempfile.NamedTemporaryFile(suffix=".exe") as executable:
            app._resolve_codex_executable = mock.Mock(return_value=executable.name)
            self.assertTrue(app._has_configured_model_provider())
        app._resolve_codex_executable.assert_called_once()

    def test_codex_usage_records_tokens_and_call_with_zero_local_cost(self):
        app = self.make_app(
            model_provider=gui_app.PROVIDER_CODEX_SOL,
            max_estimated_cost_cny=30.0,
            model_usage_ledger_enabled=True,
        )
        app._model_call_limit = 3
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            out = os.path.join(root, "output")
            os.makedirs(logs)
            os.makedirs(out)
            with mock.patch.dict(generator.DIRS, {"logs": logs, "out": out}):
                app._before_model_call()
                app._record_model_usage(
                    {
                        "prompt_tokens": 11,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 11,
                        "completion_tokens": 7,
                        "reasoning_tokens": 3,
                        "total_tokens": 18,
                    },
                    "gpt-5.6-sol",
                    False,
                    "test_codex",
                    provider=gui_app.PROVIDER_CODEX_SOL,
                    evidence={
                        "evidence_path": os.path.join(logs, "receipt.json"),
                        "evidence_sha256": "a" * 64,
                        "provenance_basis": "command_bound_official_cli",
                    },
                )
                with open(
                    os.path.join(logs, "model_usage_summary.json"),
                    "r",
                    encoding="utf-8",
                ) as handle:
                    summary = json.load(handle)
                with open(
                    os.path.join(logs, "model_usage.jsonl"),
                    "r",
                    encoding="utf-8",
                ) as handle:
                    entry = json.loads(handle.readline())
        self.assertEqual(1, app._model_call_count)
        self.assertEqual(18, summary["total_tokens"])
        self.assertEqual(1, summary["provider_calls"]["codex_sol"])
        self.assertEqual(0.0, summary["estimated_cost_cny"])
        self.assertEqual(0.0, entry["estimated_cost_cny"])
        self.assertIn("无本地可核验", entry["pricing_note"])

    def test_codex_stop_discards_complete_return(self):
        import threading

        app = self.make_app(
            model_provider=gui_app.PROVIDER_CODEX_SOL,
            max_tokens=8192,
        )
        app._stop_event = threading.Event()
        app._apply_generation_prompt_budget = lambda system, user: (
            system,
            user,
            {"final": {"input_tokens": 1, "system_chars": 1, "user_chars": 1}},
        )
        app._estimate_model_call_reserve_cny = lambda *args, **kwargs: 0.0
        app._before_model_call = lambda *args, **kwargs: None
        displayed = []
        app._ui_clear = lambda text="": displayed.append(text)
        app._ui_append = lambda text: displayed.append(text)
        app._finish_project_task = lambda: None
        app.enable_buttons = lambda *_args, **_kwargs: None
        app.update_word_count = lambda: None

        def complete_then_stop(*_args, **_kwargs):
            app._stop_event.set()
            return "第1章 不应进入界面的正文"

        app._call_codex_text = mock.Mock(side_effect=complete_then_stop)
        app.stream_call_llm("system", "user", True)

        self.assertEqual("", app.generated_content)
        self.assertTrue(any("整份丢弃" in item for item in displayed))

    def test_single_chapter_copy_can_include_or_strip_title(self):
        app = self.make_app()
        content = "\ufeff\r\n第12章 雨夜尸检\r\n\r\n第一段。\r\n第二段。\r\n"
        self.assertEqual(
            "第12章 雨夜尸检\n\n第一段。\n第二段。",
            app._chapter_copy_text(content, include_title=True),
        )
        self.assertEqual(
            "第一段。\n第二段。",
            app._chapter_copy_text(content, include_title=False),
        )

    def test_reader_lists_only_official_chapter_files(self):
        app = self.make_app()
        self.assertTrue(app._is_official_chapter_file("第0001章.txt"))
        self.assertFalse(app._is_official_chapter_file("第0001章_待审.txt"))
        self.assertFalse(app._is_official_chapter_file("第0001章_托管检查.txt"))

    def test_invalid_story_bible_never_promotes_generic_fallback(self):
        import threading

        app = self.make_app()
        app._stop_event = threading.Event()
        app._append_batch_audit = mock.Mock()
        app.call_llm_non_stream = mock.Mock(side_effect=["{}", "not json"])
        with tempfile.TemporaryDirectory() as plot_dir:
            with self.assertRaisesRegex(RuntimeError, "禁止开始托管"):
                app._generate_valid_story_bible(
                    plot_dir,
                    "测试书",
                    "悬疑",
                    30,
                    [[1, 30, "第一卷"]],
                    "全书大纲",
                    "唯一真相",
                )
            self.assertFalse(
                os.path.exists(
                    os.path.join(plot_dir, story_architect.STORY_BIBLE_FILENAME)
                )
            )
            draft_path = os.path.join(plot_dir, "story_bible_自动草案.json")
            self.assertTrue(os.path.exists(draft_path))
            with open(draft_path, "r", encoding="utf-8") as handle:
                draft = json.load(handle)
            self.assertEqual("UNVERIFIED", draft["status"])
            self.assertEqual(2, len(draft["attempts"]))
            app._append_batch_audit.assert_called_once()

    def test_story_bible_gets_one_bounded_repair_retry(self):
        import threading

        app = self.make_app()
        app._stop_event = threading.Event()
        app._append_batch_audit = mock.Mock()
        valid = story_architect.build_default_story_bible(
            "测试书", "悬疑", 30, [[1, 30, "第一卷"]]
        )
        app.call_llm_non_stream = mock.Mock(
            side_effect=["{}", json.dumps(valid, ensure_ascii=False)]
        )
        with tempfile.TemporaryDirectory() as plot_dir:
            result = app._generate_valid_story_bible(
                plot_dir,
                "测试书",
                "悬疑",
                30,
                [[1, 30, "第一卷"]],
                "全书大纲",
                "唯一真相",
            )
            self.assertEqual([], story_architect.validate_story_bible_data(result, 30))
            self.assertEqual(2, app.call_llm_non_stream.call_count)
            self.assertFalse(
                os.path.exists(os.path.join(plot_dir, "story_bible_自动草案.json"))
            )
            app._append_batch_audit.assert_not_called()

    def test_top_status_exposes_recovered_sol_drafts_without_promoting_them(self):
        app = self.make_app()
        app.current_vol = 1
        app.latest_chap = 0
        app.next_chap = 1
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            staging = os.path.join(root, "rewrite_staging", "chapters_01_30")
            os.makedirs(staging)
            for chapter in range(1, 31):
                with open(
                    os.path.join(staging, f"第{chapter:04d}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 待审稿")
            archive = os.path.join(root, "rewrite_staging", "archive")
            superseded = os.path.join(root, "rewrite_staging", "superseded")
            backup = os.path.join(root, "rewrite_staging", ".backup")
            for excluded in (archive, superseded, backup):
                os.makedirs(excluded)
                with open(
                    os.path.join(excluded, "第0031章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write("不应计入")
            with open(
                os.path.join(staging, "第0031章_待审.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("不应计入")

            self.assertEqual(list(range(1, 31)), app._recovered_staging_chapters())
            status = app.get_status_text()

        self.assertIn("正式稿尚未重建", status)
        self.assertIn("已恢复5.6-sol待审稿1—30章", status)
        self.assertIn("到单章复制查看", status)
        self.assertNotIn("已写至", status)

    def test_recovered_staging_readiness_precedes_missing_api_key(self):
        class _Widget:
            def __init__(self):
                self.values = {}

            def config(self, **kwargs):
                self.values.update(kwargs)

        app = self.make_app(api_key="YOUR_DEEPSEEK_API_KEY_HERE")
        app.latest_chap = 0
        app._active_task_name = ""
        app.readiness_lbl = _Widget()
        app.btn_full_book = _Widget()
        app._project_needs_setup = lambda: False
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            app.project_dir = root
            staging = os.path.join(root, "rewrite_staging")
            os.makedirs(staging)
            for chapter in range(1, 31):
                with open(
                    os.path.join(staging, f"第{chapter:04d}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 待审稿")

            app._refresh_readiness_status()

        self.assertEqual(
            "已恢复5.6-sol待审稿1—30章；需密钥才可重建正式链",
            app.readiness_lbl.values["text"],
        )
        self.assertEqual(
            "填写密钥后重建正式链",
            app.btn_full_book.values["text"],
        )
        self.assertNotIn("通过", app.readiness_lbl.values["text"])

    def test_staging_gap_does_not_claim_a_recovered_contiguous_range(self):
        app = self.make_app()
        app.current_vol = 1
        app.latest_chap = 0
        app.next_chap = 1
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            staging = os.path.join(root, "rewrite_staging")
            os.makedirs(staging)
            for chapter in (1, 3):
                with open(
                    os.path.join(staging, f"第{chapter:04d}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 待审稿")
            status = app.get_status_text()
        self.assertNotIn("已恢复5.6-sol待审稿", status)
        self.assertIn("尚未开始", status)

    def test_reader_choice_label_exposes_title_and_volume(self):
        """The picker should identify a chapter without opening each file."""
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "output")
            volume = os.path.join(output, "第一卷")
            os.makedirs(volume)
            path = os.path.join(volume, "第0001章.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("第1章 旧站开门\n\n林舟推开铁门。")
            with mock.patch.dict(generator.DIRS, {"out": output}, clear=False):
                label = app._reader_choice_label(path)
        self.assertIn("第0001章.txt", label)
        self.assertIn("旧站开门", label)
        self.assertIn("第一卷", label)

    def test_reader_latest_prefers_sol_staging_without_promoting_it(self):
        app = self.make_app(
            chapter_char_min=1,
            chapter_char_target_max=5000,
        )
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            write_semantic_invariants(os.path.join(root, "plot"))
            output = os.path.join(root, "output", "第一卷")
            staging = os.path.join(
                root, "rewrite_staging", "chapters_01_05"
            )
            os.makedirs(output)
            os.makedirs(staging)
            official_path = os.path.join(output, "第0001章.txt")
            staged_path = os.path.join(staging, "第0001章.txt")
            with open(official_path, "w", encoding="utf-8") as handle:
                handle.write("第1章 旧正式稿\n\n旧正文。")
            with open(staged_path, "w", encoding="utf-8") as handle:
                handle.write("第1章 新稿\n\n新正文。")
            with mock.patch.dict(generator.DIRS, {"out": os.path.dirname(output)}, clear=False):
                paths, active = app._collect_reader_chapter_files("latest")
                official_paths, official_source = app._collect_reader_chapter_files(
                    "official"
                )
                label = app._reader_choice_label(staged_path)
        self.assertEqual("latest", active)
        self.assertEqual([staged_path], paths)
        self.assertEqual("official", official_source)
        self.assertEqual([official_path], official_paths)
        self.assertIn("5.6-sol待审稿", label)

    def test_reader_latest_merges_staging_overrides_with_official_fallbacks(self):
        app = self.make_app(
            chapter_char_min=1,
            chapter_char_target_max=5000,
        )
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            write_semantic_invariants(os.path.join(root, "plot"))
            output = os.path.join(root, "output", "第一卷")
            staging = os.path.join(root, "rewrite_staging", "chapters_01_05")
            os.makedirs(output)
            os.makedirs(staging)
            official_paths = {}
            for chapter in (1, 2, 3):
                path = os.path.join(output, f"第{chapter:04d}章.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(f"第{chapter}章 正式稿\n\n正式正文。")
                official_paths[chapter] = path
            staged_paths = {}
            for chapter in (2, 4):
                path = os.path.join(staging, f"第{chapter:04d}章.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(f"第{chapter}章 暂存改写\n\n暂存正文。")
                staged_paths[chapter] = path
            with mock.patch.dict(
                generator.DIRS, {"out": os.path.dirname(output)}, clear=False
            ):
                paths, active = app._collect_reader_chapter_files("latest")
                labels = [app._reader_choice_label(path) for path in paths]
        self.assertEqual("latest", active)
        self.assertEqual(
            [
                official_paths[1],
                staged_paths[2],
                official_paths[3],
                staged_paths[4],
            ],
            paths,
        )
        self.assertIn("[正式稿]", labels[0])
        self.assertIn("[5.6-sol待审稿]", labels[1])
        self.assertIn("[正式稿]", labels[2])
        self.assertIn("[5.6-sol待审稿]", labels[3])

    def test_reader_hides_incomplete_sol_staging_chapter(self):
        app = self.make_app(
            chapter_char_min=3200,
            chapter_char_target_max=3800,
        )
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            write_semantic_invariants(os.path.join(root, "plot"))
            staging = os.path.join(
                root, "rewrite_staging", "chapters_21_25"
            )
            os.makedirs(staging)
            with open(
                os.path.join(staging, "第0021章.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("第21章 尚未写完\n\n这只是正在生成的半章。")
            paths, active = app._collect_reader_chapter_files("staging")
        self.assertEqual("staging", active)
        self.assertEqual([], paths)

    def test_reader_copy_uses_visible_text_when_user_edits_reader(self):
        app = self.make_app()

        class _ReaderText:
            def get(self, *_args):
                return "第1章 修订标题\n\n这是修改后的正文。\n"

        app.reader_text = _ReaderText()
        app.reader_chapter_files = []
        app._reader_loaded_path = os.path.abspath("第0001章.txt")
        app._reader_loaded_content = "第1章 原标题\n\n这是原始正文。\n"
        self.assertEqual(
            "第1章 修订标题\n\n这是修改后的正文。",
            app._chapter_copy_text(app._current_reader_content(), include_title=True),
        )
        self.assertEqual(
            "local_edit_unreviewed", app._reader_copy_provenance_state
        )
        self.assertEqual(
            "[注意]本地编辑未审查", app._reader_copy_provenance_label()
        )

    def test_reader_copy_marks_unmodified_official_disk_snapshot(self):
        app = self.make_app()

        class _ReaderText:
            def __init__(self, text):
                self.text = text

            def get(self, *_args):
                return self.text

        content = "第1章 原标题\n\n这是正式正文。\n"
        with tempfile.TemporaryDirectory() as output:
            path = os.path.join(output, "第0001章.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            app.reader_text = _ReaderText(content)
            app.reader_chapter_files = [path]
            app.reader_chapter_idx = 0
            app._reader_loaded_path = os.path.abspath(path)
            app._reader_loaded_content = content
            app.project_dir = os.path.dirname(output)
            with mock.patch.dict(generator.DIRS, {"out": output}, clear=False):
                self.assertEqual(content.strip(), app._current_reader_content())
        self.assertEqual(
            "official_disk_exact", app._reader_copy_provenance_state
        )
        self.assertEqual(
            "[正式稿·与磁盘一致]", app._reader_copy_provenance_label()
        )

    def test_new_project_folder_does_not_overwrite_title_file(self):
        """A legacy file with the new title must not block isolated setup."""
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            projects = os.path.join(root, "projects")
            os.makedirs(projects)
            collision = os.path.join(projects, "新书")
            with open(collision, "w", encoding="utf-8") as handle:
                handle.write("legacy marker")
            with mock.patch("gui_app._exe_dir", root):
                selected = app._unique_project_folder("新书")
            self.assertNotEqual(selected, collision)
            self.assertTrue(selected.startswith(projects))
            self.assertTrue(os.path.isfile(collision))

    def test_long_outline_containing_template_word_is_meaningful(self):
        outline = (
            "第21章 身份模板失效\n"
            "核心事件：主角发现多名选手共用同一身份模板，必须沿现实证据继续追查。\n"
            "冲突/爽点：数据库记录与尸检旧伤互相矛盾，案件由单人冒名升级为身份租赁链。\n"
            "章末钩子：系统中的死者正在另一座擂台完成资格赛。\n"
        ) * 3
        self.assertTrue(book_initializer.has_meaningful_content(outline))

    def test_environment_key_is_runtime_only(self):
        app = self.make_app(
            api_key="env-secret",
            model="deepseek-v4-flash",
            review_model="deepseek-v4-pro",
        )
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-secret"}, clear=True):
            self.assertEqual(app._get_runtime_api_key(), "env-secret")
            self.assertEqual(
                app._global_config_payload()["api_key"],
                "YOUR_DEEPSEEK_API_KEY_HERE",
            )

    def test_unconfigured_project_requires_setup(self):
        app = self.make_app(
            book_title="我的新小说",
            book_brief="",
            target_total_chapters=300,
        )
        self.assertTrue(app._project_needs_setup())
        app.config.update(
            book_title="新书",
            book_brief="一个明确且可执行的故事主题",
            target_total_chapters=120,
        )
        self.assertFalse(app._project_needs_setup())

    def test_volume_ranges_cover_the_whole_book(self):
        app = self.make_app()
        self.assertEqual(
            app._build_volume_ranges(23, 10),
            [[1, 10, "第一卷"], [11, 20, "第二卷"], [21, 23, "第三卷"]],
        )

    def test_trial_limit_is_separate_from_full_book_plan(self):
        app = self.make_app(
            target_total_chapters=360,
            volume_ranges=[[1, 60, "第一卷"], [61, 120, "第二卷"]],
            trial_stop_chapter=30,
            trial_continue_confirmed=False,
        )
        self.assertTrue(app._trial_mode_pending())
        self.assertEqual(app._get_generation_target_end(), 30)
        app.config["trial_continue_confirmed"] = True
        self.assertFalse(app._trial_mode_pending())
        self.assertEqual(app._get_generation_target_end(), 360)

    def test_simple_trial_gate_uses_formal_integrity_without_model_receipt(self):
        app = self.make_app(
            release_mode="simple",
            trial_stop_chapter=30,
            target_total_chapters=300,
        )
        app._load_latest_commercial_review = mock.Mock(
            side_effect=AssertionError("简单模式不应读取商业 PASS 凭证")
        )
        app._hard_gate_integrity_issues = lambda _chapter: []
        self.assertTrue(app._trial_gate_ready())
        app._load_latest_commercial_review.assert_not_called()

        app._hard_gate_integrity_issues = lambda _chapter: ["第12章状态哈希不一致"]
        self.assertFalse(app._trial_gate_ready())

    def test_current_pending_draft_is_retryable_not_a_preflight_failure(self):
        app = self.make_app(release_mode="simple")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"out": root}, clear=False
        ):
            with open(
                os.path.join(root, "第0040章_待审.txt"), "w", encoding="utf-8"
            ) as handle:
                handle.write("当前章的可重试草稿")
            app._official_chapter_paths_by_number = lambda: {
                chapter: os.path.join(root, f"第{chapter:04d}章.txt")
                for chapter in range(1, 40)
            }
            retryable, blocking = app._classify_pending_review_paths(40)
            self.assertEqual(1, len(retryable))
            self.assertEqual([], blocking)

            with open(
                os.path.join(root, "第0041章_待审.txt"), "w", encoding="utf-8"
            ) as handle:
                handle.write("超前草稿")
            retryable, blocking = app._classify_pending_review_paths(40)
            self.assertEqual(1, len(retryable))
            self.assertEqual(1, len(blocking))

    def test_live_batch_progress_refreshes_lightweight_chapter_label(self):
        app = self.make_app()
        app.current_vol = 2
        app.latest_chap = 191
        app.next_chap = 192
        app.info_lbl = mock.Mock()
        app._ui = lambda callback: callback()

        app._refresh_live_progress_label()

        app.info_lbl.config.assert_called_once_with(
            text="进度：第 2 卷 | 已写至：第 191 章 | 下一章：第 192 章"
        )

    def test_author_layer_chapter_references_are_repaired_locally(self):
        app = self.make_app()
        content = (
            "第192章 接收之后\n\n"
            "第182章生效的分层规程允许拆分控制。"
            "上一章留下的记录会在下一章继续核验。"
        )

        sanitized, repairs = app._sanitize_author_layer_chapter_references(
            content
        )

        self.assertTrue(sanitized.startswith("第192章 接收之后\n"))
        self.assertIn("此前生效的分层规程", sanitized)
        self.assertIn("此前留下的记录会在随后继续核验", sanitized)
        self.assertNotIn("第182章", sanitized)
        self.assertTrue(repairs)
        self.assertFalse(narrative_guard.deterministic_issues(sanitized))

    def test_simple_commercial_review_config_is_lightweight_and_advisory(self):
        app = self.make_app(
            release_mode="simple",
            commercial_hard_gate_chapters=[3, 30],
            golden_three_require_explicit_continue=True,
            commercial_hard_gate_require_explicit_continue=True,
            commercial_review_volume_end=True,
            commercial_review_lookback=30,
            commercial_review_fulltext_chars=120000,
        )
        review_config = app._commercial_review_runtime_config()
        self.assertEqual([-1], review_config["commercial_hard_gate_chapters"])
        self.assertFalse(review_config["golden_three_require_explicit_continue"])
        self.assertFalse(
            review_config["commercial_hard_gate_require_explicit_continue"]
        )
        self.assertFalse(review_config["commercial_review_volume_end"])
        self.assertEqual(10, review_config["commercial_review_lookback"])
        self.assertEqual(40000, review_config["commercial_review_fulltext_chars"])
        self.assertEqual([3, 30], app.config["commercial_hard_gate_chapters"])

    def test_simple_truth_anchors_accept_structured_sources(self):
        app = self.make_app(release_mode="simple")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root}, clear=True
        ):
            for name in (
                "story_bible.json",
                continuity_guard.CANON_FILENAME,
                "semantic_invariants.json",
                "关键事实库.json",
            ):
                with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                    handle.write('{"ready": true}')
            self.assertEqual(
                "PASS", app._truth_anchor_health_status("时间线锚点.txt")
            )
            self.assertEqual(
                "PASS", app._truth_anchor_health_status("伏笔与因果追踪表.txt")
            )

        strict = self.make_app(release_mode="strict")
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root}, clear=True
        ):
            self.assertEqual(
                "FAIL", strict._truth_anchor_health_status("时间线锚点.txt")
            )

    def test_stale_sol_rebuild_state_self_corrects_from_official_progress(self):
        app = self.make_app(target_total_chapters=300)
        with tempfile.TemporaryDirectory() as root:
            state_path = os.path.join(root, "batch_state.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "status": "paused",
                    "mode": "trial",
                    "target_end": 30,
                    "completed_count": 0,
                    "resume_allowed": False,
                    "stage": "sol_staging_content_review_passed_needs_formal_rebuild",
                    "message": "正式 output/state 尚未重建",
                }, handle, ensure_ascii=False)
            app._batch_state_path = lambda: state_path
            with mock.patch(
                "gui_app.generator.get_latest_chapter_info",
                return_value=("第一卷", 31, "", 30, ""),
            ):
                state = app._load_batch_state()
            self.assertEqual("ready", state["status"])
            self.assertEqual("full_book", state["mode"])
            self.assertEqual(300, state["target_end"])
            self.assertEqual(30, state["completed_count"])
            self.assertEqual("official_chapters_verified", state["stage"])
            with open(state_path, "r", encoding="utf-8") as handle:
                persisted = json.load(handle)
            self.assertEqual(state["stage"], persisted["stage"])

    def test_new_batch_does_not_inherit_previous_job_details(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            state_path = os.path.join(root, "batch_state.json")
            app._batch_state_path = lambda: state_path
            app._write_batch_state(job_id="old", status="paused", finished_at="yesterday",
                                   current_chapter=65, message="old failure")
            app._write_batch_state(job_id="new", status="running", stage="starting")
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual("new", state["job_id"])
            for field in ("finished_at", "current_chapter", "message"):
                self.assertNotIn(field, state)

    def test_resuming_same_batch_retains_context_but_clears_finished_time(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            state_path = os.path.join(root, "batch_state.json")
            app._batch_state_path = lambda: state_path
            app._write_batch_state(job_id="same", status="interrupted", finished_at="old",
                                   target_end=70, network_resume_cycles=1)
            app._write_batch_state(job_id="same", status="running")
            app._write_batch_state(stage="generating", current_chapter=66)
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertNotIn("finished_at", state)
            self.assertEqual(70, state["target_end"])
            self.assertEqual(1, state["network_resume_cycles"])
            self.assertEqual(66, state["current_chapter"])

    def test_batch_state_recovers_from_non_object_json(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            state_path = os.path.join(root, "batch_state.json")
            app._batch_state_path = lambda: state_path
            with open(state_path, "w", encoding="utf-8") as handle:
                handle.write("[]")
            app._write_batch_state(job_id="new", status="running")
            with open(state_path, encoding="utf-8") as handle:
                self.assertEqual("new", json.load(handle)["job_id"])

    def test_trial_gate_requires_pass_status_and_no_old_debt(self):
        app = self.make_app(
            target_total_chapters=360,
            volume_ranges=[[1, 60, "第一卷"]],
            trial_stop_chapter=30,
            trial_continue_confirmed=False,
        )
        raw_review = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 可以继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 已核验试写闸门"
        )
        review = {
            "chapter": 30,
            "gate_blocked": False,
            "model": "deepseek-v4-pro",
            "reviewed_chapters": list(range(1, 31)),
            "expected_chapters": list(range(1, 31)),
            "chapter_hashes": {
                str(chapter): f"{chapter:064x}"
                for chapter in range(1, 31)
            },
            **commercial_reviewer.parse_review(raw_review),
        }
        review["review_receipt"] = commercial_reviewer.build_review_receipt(
            "自动商业审查", "审查一至三十章", raw_review, "deepseek-v4-pro",
            list(range(1, 31)),
            chapter_hashes=review["chapter_hashes"],
        )
        app._load_latest_commercial_review = lambda: review
        app._load_latest_chapter_statuses = lambda: {
            30: {"status": "正式可用"}
        }
        app._hard_gate_integrity_issues = lambda _chapter: []
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root}, clear=True
        ), mock.patch(
            "gui_app.commercial_reviewer.load_revision_debt",
            return_value={
                "items": [
                    {"status": "OPEN", "opened_chapter": 30},
                ]
            },
        ):
            self.assertTrue(app._trial_gate_ready())
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root}, clear=True
        ), mock.patch(
            "gui_app.commercial_reviewer.load_revision_debt",
            return_value={
                "items": [
                    {"status": "OPEN", "opened_chapter": 10},
                ]
            },
        ):
            self.assertFalse(app._trial_gate_ready())

    def test_legacy_project_at_chapter_30_requires_gate_migration(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "output", "第一卷")
            os.makedirs(output)
            with open(
                os.path.join(output, "第0030章.txt"), "w", encoding="utf-8"
            ) as handle:
                handle.write("第30章 旧项目正文")
            with open(
                os.path.join(root, "project_config.json"), "w", encoding="utf-8"
            ) as handle:
                json.dump(
                    {
                        "book_title": "旧项目",
                        "book_brief": "旧项目已经运行到第三十章",
                        "target_total_chapters": 300,
                    },
                    handle,
                    ensure_ascii=False,
                )
            app._load_project_config(root)
            self.assertFalse(app.config["trial_continue_confirmed"])
            self.assertTrue(app.config["trial_gate_migration_required"])

    def test_model_call_budget_is_a_hard_limit(self):
        app = self.make_app()
        app._model_call_limit = 2
        app._before_model_call()
        app._before_model_call()
        with self.assertRaisesRegex(RuntimeError, "硬上限 2 次"):
            app._before_model_call()
        self.assertEqual(app._model_call_count, 2)

    def test_per_chapter_model_call_budget_resets_for_next_chapter(self):
        app = self.make_app(max_model_calls_per_chapter=4)
        app._model_call_limit = 20
        app._begin_chapter_model_budget(7)
        for _ in range(4):
            app._before_model_call()
        with self.assertRaisesRegex(RuntimeError, "单章硬上限 4 次"):
            app._before_model_call()
        app._begin_chapter_model_budget(8)
        app._before_model_call()
        self.assertEqual(app._chapter_model_call_count, 1)
        self.assertEqual(app._model_call_count, 5)

    def test_per_chapter_budget_reserves_calls_for_evidence_ledger(self):
        app = self.make_app(
            max_model_calls_per_chapter=16,
            structured_state_enabled=True,
            state_delta_max_attempts=2,
            state_delta_independent_audit=True,
        )
        app._model_call_limit = 100
        app._begin_chapter_model_budget(9)
        for _ in range(12):
            app._before_model_call()
        with self.assertRaisesRegex(RuntimeError, "预留 4 次"):
            app._before_model_call()
        app._state_ledger_call_active = True
        for _ in range(4):
            app._before_model_call()
        with self.assertRaisesRegex(RuntimeError, "单章硬上限 16 次"):
            app._before_model_call()

    def test_commercial_hard_gate_has_exact_logical_and_fallback_budgets(self):
        app = self.make_app(
            max_model_calls_per_chapter=16,
            structured_state_enabled=True,
            state_delta_max_attempts=2,
            state_delta_independent_audit=True,
        )
        app._model_call_limit = 100
        app._begin_chapter_model_budget(300)
        for _ in range(12):
            app._before_model_call()

        app._commercial_review_call_active = True
        app._commercial_review_stage_remaining = 3
        app._commercial_review_fallback_eligible = 0
        for _ in range(3):
            app._before_model_call()
            app._commercial_review_fallback_call_active = True
            app._before_model_call()
            app._commercial_review_fallback_call_active = False

        self.assertEqual(0, app._commercial_review_stage_remaining)
        self.assertEqual(0, app._commercial_review_fallback_eligible)
        with self.assertRaisesRegex(RuntimeError, "分块预算"):
            app._before_model_call()
        app._commercial_review_fallback_call_active = True
        with self.assertRaisesRegex(RuntimeError, "回退额度"):
            app._before_model_call()

    def test_commercial_hard_gate_still_obeys_whole_job_limit(self):
        app = self.make_app(max_model_calls_per_chapter=16)
        app._model_call_limit = 1
        app._commercial_review_call_active = True
        app._commercial_review_stage_remaining = 5
        app._before_model_call()
        with self.assertRaisesRegex(RuntimeError, "硬上限 1 次"):
            app._before_model_call()
        self.assertEqual(4, app._commercial_review_stage_remaining)

    def test_project_cost_stop_survives_restart_via_usage_summary(self):
        app = self.make_app(max_estimated_cost_cny=10.0)
        app._model_call_limit = 100
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with open(
                os.path.join(logs, "model_usage_summary.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump({"estimated_cost_cny": 10.01}, handle)
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "已达到止损"):
                    app._before_model_call()
            self.assertEqual(0, app._model_call_count)

    def test_project_cost_below_stop_allows_next_call(self):
        app = self.make_app(max_estimated_cost_cny=10.0)
        app._model_call_limit = 100
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with open(
                os.path.join(logs, "model_usage_summary.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump({"estimated_cost_cny": 9.99}, handle)
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                app._before_model_call()
            self.assertEqual(1, app._model_call_count)

    def test_recovered_external_cost_is_hash_bound_and_counted(self):
        with tempfile.TemporaryDirectory() as root:
            evidence_rel = os.path.join("review_reports", "cost.md")
            evidence = os.path.join(root, evidence_rel)
            os.makedirs(os.path.dirname(evidence))
            with open(evidence, "w", encoding="utf-8") as handle:
                handle.write("项目外部模型账本估算 ¥19.18072444")
            with open(evidence, "rb") as evidence_file:
                digest = hashlib.sha256(evidence_file.read()).hexdigest()
            app = self.make_app(
                max_estimated_cost_cny=30.0,
                recovered_external_cost_cny=19.18072444,
                recovered_external_cost_evidence=evidence_rel,
                recovered_external_cost_evidence_sha256=digest,
            )
            app.project_dir = root
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                self.assertAlmostEqual(19.18072444, app._project_estimated_cost_cny())
                app._before_model_call(reserved_cost_cny=0.05)
            self.assertEqual(1, app._model_call_count)

    def test_changed_recovered_cost_evidence_blocks_calls(self):
        with tempfile.TemporaryDirectory() as root:
            evidence_rel = os.path.join("review_reports", "cost.md")
            evidence = os.path.join(root, evidence_rel)
            os.makedirs(os.path.dirname(evidence))
            with open(evidence, "w", encoding="utf-8") as handle:
                handle.write("项目外部模型账本估算 ¥19.18072444")
            app = self.make_app(
                max_estimated_cost_cny=30.0,
                recovered_external_cost_cny=19.18072444,
                recovered_external_cost_evidence=evidence_rel,
                recovered_external_cost_evidence_sha256="0" * 64,
            )
            app.project_dir = root
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "费用账本损坏"):
                    app._before_model_call(reserved_cost_cny=0.05)
            self.assertEqual(0, app._model_call_count)

    def test_model_call_reservation_blocks_single_call_overshoot(self):
        app = self.make_app(max_estimated_cost_cny=30.0)
        app._model_call_limit = 100
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with open(
                os.path.join(logs, "model_usage_summary.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump({"estimated_cost_cny": 29.99}, handle)
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "调用前停止"):
                    app._before_model_call(reserved_cost_cny=0.02)
            self.assertEqual(0, app._model_call_count)

    def test_corrupt_usage_summary_blocks_next_call(self):
        app = self.make_app(max_estimated_cost_cny=10.0)
        app._model_call_limit = 100
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with open(
                os.path.join(logs, "model_usage_summary.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("{broken")
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "费用账本损坏"):
                    app._before_model_call()
        self.assertEqual(0, app._model_call_count)

    def test_nonempty_usage_ledger_without_summary_blocks_next_call(self):
        app = self.make_app(max_estimated_cost_cny=10.0)
        app._model_call_limit = 100
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with open(
                os.path.join(logs, "model_usage.jsonl"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write('{"estimated_cost_cny": 1.25}\n')
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "费用账本损坏"):
                    app._before_model_call()
        self.assertEqual(0, app._model_call_count)

    def test_usage_summary_and_ledger_call_mismatch_blocks_after_restart(self):
        app = self.make_app(max_estimated_cost_cny=10.0)
        app._model_call_limit = 100
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            os.makedirs(logs)
            with open(
                os.path.join(logs, "model_usage_summary.json"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    {"estimated_cost_cny": 1.0, "calls_with_usage": 1},
                    handle,
                )
            with open(
                os.path.join(logs, "model_usage.jsonl"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write('{"estimated_cost_cny": 0.5}\n')
                handle.write('{"estimated_cost_cny": 0.5}\n')
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "费用账本损坏"):
                    app._before_model_call()
        self.assertEqual(0, app._model_call_count)

    def test_usage_summary_write_failure_blocks_following_call(self):
        app = self.make_app(
            max_estimated_cost_cny=10.0,
            model_usage_ledger_enabled=True,
        )
        app._model_call_limit = 100
        app._append_batch_audit = lambda payload: None
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True), mock.patch(
                "gui_app.deepseek_runtime.estimate_cost_cny", return_value=1.25
            ), mock.patch("gui_app._atomic_write_text", side_effect=OSError("disk full")):
                app._record_model_usage(
                    usage, "deepseek-v4-pro", False, "test_review"
                )
                self.assertEqual(1.25, app._project_estimated_cost_cny())
                with self.assertRaisesRegex(RuntimeError, "写入失败"):
                    app._before_model_call()
        self.assertEqual(0, app._model_call_count)

    def test_missing_usage_summary_allows_call_and_success_is_not_double_counted(self):
        app = self.make_app(
            max_estimated_cost_cny=10.0,
            model_usage_ledger_enabled=True,
        )
        app._model_call_limit = 100
        app._append_batch_audit = lambda payload: None
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        with tempfile.TemporaryDirectory() as root:
            logs = os.path.join(root, "logs")
            with mock.patch.dict(generator.DIRS, {"logs": logs}, clear=True), mock.patch(
                "gui_app.deepseek_runtime.estimate_cost_cny", return_value=1.25
            ):
                app._before_model_call()
                app._record_model_usage(
                    usage, "deepseek-v4-pro", False, "test_review"
                )
                self.assertEqual(1.25, app._project_estimated_cost_cny())
                with open(
                    os.path.join(logs, "model_usage_summary.json"),
                    "r",
                    encoding="utf-8",
                ) as handle:
                    summary = json.load(handle)
        self.assertEqual(1.25, summary["estimated_cost_cny"])
        self.assertEqual(0.0, app._model_usage_unpersisted_cost_cny)

    def test_project_cache_reset_clears_usage_persistence_fault(self):
        app = self.make_app()
        app._model_usage_persistence_fault = "disk full"
        app._model_usage_unpersisted_cost_cny = 1.25
        app._reset_project_scoped_caches()
        self.assertEqual("", app._model_usage_persistence_fault)
        self.assertEqual(0.0, app._model_usage_unpersisted_cost_cny)

    def test_commercial_hard_gate_gets_thinking_plus_verdict_budget(self):
        app = self.make_app(
            max_tokens=8192,
            max_estimated_cost_cny=0,
            model_usage_ledger_enabled=False,
            deepseek_review_thinking=True,
            deepseek_review_thinking_min_tokens=6000,
            deepseek_review_reasoning_effort="high",
            commercial_review_lookback=30,
            commercial_review_fulltext_chars=120000,
            commercial_review_volume_end=False,
            golden_three_require_explicit_continue=True,
            golden_three_gate_chapter=3,
            target_total_chapters=30,
        )
        requests = []
        passed_limits = []
        passing = (
            "OVERALL: PASS\nCURRENT: PASS\nACTION: CONTINUE\n"
            "SUMMARY: 可以继续\nDEBT_STATUS: RESOLVED\n"
            "DEBT_EVIDENCE:\n- 已核验试写闸门"
        )

        def create(**kwargs):
            requests.append(kwargs)
            return types.SimpleNamespace(
                usage=None,
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(content=passing)
                    )
                ],
            )

        app.get_client = lambda: types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)
            )
        )
        app.get_review_model_name = lambda: "deepseek-v4-pro"
        app._get_volume_ranges = lambda: []
        app._get_story_planned_end_chapter = lambda: 30
        app._commercial_contract_context = lambda max_chars=5200: "商业契约"
        original_call = app.call_llm_review

        def capture_call(system_prompt, user_prompt, **kwargs):
            passed_limits.append(kwargs.get("max_tokens"))
            return original_call(system_prompt, user_prompt, **kwargs)

        app.call_llm_review = capture_call
        with tempfile.TemporaryDirectory() as root:
            output = os.path.join(root, "output")
            plot = os.path.join(root, "plot")
            os.makedirs(output)
            os.makedirs(plot)
            for chapter in (1, 2):
                with open(
                    os.path.join(output, f"第{chapter:04d}章.txt"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(f"第{chapter}章 正文\n\n调查推进。")
            app.project_dir = root
            with mock.patch.dict(
                generator.DIRS, {"out": output, "plot": plot}, clear=True
            ), mock.patch(
                "gui_app.story_architect.build_story_context", return_value="故事契约"
            ), mock.patch(
                "gui_app.continuity_guard.build_canon_context", return_value="正史"
            ):
                result = app._run_commercial_stage_review(
                    "第3章 正文\n\n线索落地。",
                    3,
                    persist_report=False,
                )
        self.assertEqual("PASS", result["status"])
        self.assertEqual([8600], passed_limits)
        self.assertEqual(8600, requests[0]["max_tokens"])
        self.assertEqual(
            {"type": "enabled"}, requests[0]["extra_body"]["thinking"]
        )

    def test_gui_commits_state_only_after_independent_audit_passes(self):
        app = self.make_app(
            structured_state_enabled=True,
            state_delta_max_attempts=2,
            state_delta_independent_audit=True,
            subplot_stale_warn_chapters=8,
        )
        app._append_batch_audit = lambda payload: None
        chapter = "第1章 旧站\n\n林舟在旧站醒来。他拿起一把铜钥匙。"
        delta = {
            "chapter": 1,
            "characters": [{
                "name": "林舟", "location": "旧站", "condition": "", "emotion": "",
                "status": "active", "knowledge_add": [], "abilities_add": [],
                "evidence_quote": "林舟在旧站醒来。",
            }],
            "resources": [{
                "owner": "林舟", "item": "铜钥匙", "action": "GAIN",
                "quantity_change": 1, "quantity": None, "status": "available",
                "evidence_quote": "他拿起一把铜钥匙。",
            }],
            "relationships": [], "hooks": [], "subplots": [],
            "events": [{
                "event_type": "resource", "summary": "林舟取得铜钥匙",
                "evidence_quote": "他拿起一把铜钥匙。",
            }],
        }
        responses = iter([
            json.dumps(delta, ensure_ascii=False),
            json.dumps({"pass": True, "reject": []}, ensure_ascii=False),
        ])
        app.call_llm_review = lambda *args, **kwargs: next(responses)
        with tempfile.TemporaryDirectory() as temp_dir:
            plot_dir = os.path.join(temp_dir, "plot")
            os.makedirs(plot_dir)
            with mock.patch.dict(generator.DIRS, {"plot": plot_dir}):
                state = app._update_structured_state_ledger(1, chapter, "林舟在旧站醒来")
            self.assertEqual(state["current_chapter"], 1)
            self.assertTrue(os.path.exists(os.path.join(plot_dir, "story_state.json")))
            self.assertTrue(os.path.exists(os.path.join(plot_dir, "state_deltas", "chapter_0001.json")))

    def test_progression_forces_independent_audit_even_when_optional_audits_disabled(self):
        for passes in (True, False):
            with self.subTest(passes=passes), tempfile.TemporaryDirectory() as plot_dir:
                app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                    state_delta_independent_audit=False, pre_save_continuity_audit_enabled=False,
                    temporal_memory_enabled=False)
                app._append_batch_audit = lambda payload: None
                chapter = "林舟收到回执，调查员任职手续已经完成。"
                stage = "正式调查员"
                continuity_guard.save_canon(plot_dir, {"protagonist": "林舟",
                    "realm_order": ["见习调查员", stage], "current_realm": "见习调查员"})
                delta = {"chapter": 1, "characters": [{"name": "林舟",
                    "progression": stage, "evidence_quote": chapter}],
                    "events": [{"event_type": "reveal", "summary": chapter,
                                "evidence_quote": chapter}]}
                audit = {"pass": passes, "missing": [], "reject": [] if passes else [{
                    "path": "characters[0]", "reason": "阶段证据未通过独立审核",
                    "draft_quote": chapter}]}
                app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta, ensure_ascii=False),
                                                            json.dumps(audit, ensure_ascii=False)])
                with mock.patch.dict(generator.DIRS, {"plot": plot_dir}):
                    if passes:
                        prepared = app._prepare_structured_state_delta(1, chapter)
                        self.assertEqual(stage, prepared["delta"]["characters"][0]["progression"])
                    else:
                        with self.assertRaisesRegex(Exception, "阶段证据未通过"):
                            app._prepare_structured_state_delta(1, chapter)
                self.assertEqual(2, app.call_llm_review.call_count)
                for call in app.call_llm_review.call_args_list:
                    self.assertIn(stage, call.args[1])
                self.assertFalse(os.path.exists(os.path.join(plot_dir, "story_state.json")))

    def test_last_extraction_rejection_cannot_be_pruned_into_success(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '第1章 旧站\n\n林舟用完最后一次尝试，剩余次数归零。林舟离开旧站。'
        delta = {'chapter': 1, 'characters': [], 'relationships': [], 'hooks': [], 'subplots': [],
                 'resources': [{'owner': '林舟', 'item': '尝试次数', 'action': 'SET', 'quantity': 1,
                                'quantity_change': None, 'status': '',
                                'evidence_quote': '林舟用完最后一次尝试，剩余次数归零。'}],
                 'events': [{'event_type': 'investigation', 'summary': '林舟离开旧站',
                             'evidence_quote': '林舟离开旧站。'}]}
        audit = {'pass': False, 'reject': [{'path': 'resources[0]', 'reason': '余额应为零',
                                           'draft_quote': '林舟用完最后一次尝试，剩余次数归零。'}]}
        responses = iter([json.dumps(delta, ensure_ascii=False), json.dumps(audit, ensure_ascii=False)])
        app.call_llm_review = lambda *args, **kwargs: next(responses)
        with tempfile.TemporaryDirectory() as root:
            plot_dir = os.path.join(root, 'plot')
            os.makedirs(plot_dir)
            with mock.patch.dict(generator.DIRS, {'plot': plot_dir}):
                with self.assertRaisesRegex(Exception, '余额应为零'):
                    app._prepare_structured_state_delta(1, chapter, '林舟消耗尝试后离开')
            self.assertFalse(os.path.exists(os.path.join(plot_dir, 'story_state.json')))

    def test_state_retry_rechecks_auditor_suggestion_against_original_order(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '林舟先转身，再看见站长。'
        delta = {'chapter': 1, 'events': [{'event_type': 'world',
                 'summary': '林舟先转身，再看见站长', 'evidence_quote': chapter}]}
        responses = iter([json.dumps(delta, ensure_ascii=False), json.dumps({
            'pass': False, 'reject': [{'path': 'events[0]', 'reason': '请改摘要',
                'draft_quote': chapter, 'repair_instruction': '改为看见站长后转身'}]}, ensure_ascii=False),
            json.dumps(delta, ensure_ascii=False), json.dumps({'pass': True, 'reject': []})])
        requests = []
        def review(system, prompt, **kwargs):
            requests.append(prompt)
            return next(responses)
        app.call_llm_review = review
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(generator.DIRS, {'plot': root}):
                app._prepare_structured_state_delta(1, chapter)
        self.assertEqual(4, len(requests))
        self.assertNotIn('必须原样采用该中性表述', requests[2])
        self.assertIn('审计修复建议不是新增正史', requests[2])
        self.assertIn('不得倒置动作先后', requests[2])

    def test_state_audit_call_failure_stops_without_extraction_retry(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        logs, contexts = [], []
        app._append_batch_audit = logs.append
        chapter = '林舟离开旧站。'
        delta = {'chapter': 1, 'events': [{'event_type': 'world',
                 'summary': '林舟离开旧站', 'evidence_quote': chapter}]}
        app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), RuntimeError('SECRET')])
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(generator.DIRS, {'plot': root}):
                with self.assertRaisesRegex(Exception, '独立证据审计调用未完成') as raised:
                    app._prepare_structured_state_delta(1, chapter, rejection_callback=contexts.append)
            self.assertFalse(os.path.exists(os.path.join(root, 'story_state.json')))
        self.assertEqual(2, app.call_llm_review.call_count)
        self.assertEqual([], contexts)
        self.assertEqual('structured_state_audit_unavailable', logs[-1]['event'])
        self.assertNotIn('SECRET', str(raised.exception))
        self.assertFalse(app._state_ledger_call_active)

    def test_state_audit_outage_preserves_last_content_rejection_and_full_hashes(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        logs, contexts = [], []
        app._append_batch_audit = logs.append
        chapter = '林舟离开旧站。'
        delta = {'chapter': 1, 'events': [{'event_type': 'world',
                 'summary': '林舟离开旧站', 'evidence_quote': chapter}]}
        reject = {'pass': False, 'reject': [{'path': 'events[0]',
                  'reason': '需要复核摘要', 'draft_quote': chapter}]}
        error = ('Codex exec failed with exit code 1; failure_hint=service_capacity; stdout_sha256='
                 + 'b' * 64 + '; stderr_sha256=' + 'c' * 64 + '; SECRET')
        app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(reject),
                                                     json.dumps(delta), RuntimeError(error)])
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(generator.DIRS, {'plot': root}):
                with self.assertRaisesRegex(Exception, '独立证据审计调用未完成') as raised:
                    app._prepare_structured_state_delta(1, chapter, rejection_callback=contexts.append)
        self.assertEqual(4, app.call_llm_review.call_count)
        self.assertEqual(1, len(contexts))
        self.assertIn('需要复核摘要', str(contexts[0]['issues']))
        self.assertEqual('c' * 64, logs[-1]['diagnostic']['stderr_sha256'])
        self.assertEqual('service_capacity', logs[-1]['diagnostic']['failure_hint'])
        self.assertIn('c' * 64, str(raised.exception))
        self.assertNotIn('SECRET', str(raised.exception))

    def test_state_rejection_context_resumes_without_becoming_an_approval(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '第1章 旧站\n\n林舟用完最后一次尝试，剩余次数归零。'
        wrong = {'chapter': 1, 'characters': [], 'relationships': [], 'hooks': [], 'subplots': [],
                 'resources': [{'owner': '林舟', 'item': '尝试次数', 'action': 'SET', 'quantity': 1,
                                'quantity_change': None, 'status': '', 'evidence_quote': chapter.splitlines()[-1]}],
                 'events': [{'event_type': 'world', 'summary': '林舟用完最后一次尝试',
                             'evidence_quote': chapter.splitlines()[-1]}]}
        fixed = json.loads(json.dumps(wrong, ensure_ascii=False))
        fixed['resources'][0]['quantity'] = 0
        first = iter([json.dumps(wrong, ensure_ascii=False), json.dumps({
            'pass': False, 'reject': [{'path': 'resources[0]', 'reason': '余额应为零',
                'draft_quote': chapter.splitlines()[-1]}]}, ensure_ascii=False)])
        app.call_llm_review = lambda *args, **kwargs: next(first)
        contexts = []
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(generator.DIRS, {'plot': root}):
                with self.assertRaisesRegex(Exception, '余额应为零'):
                    app._prepare_structured_state_delta(
                        1, chapter, rejection_callback=contexts.append)
                self.assertFalse(os.path.exists(os.path.join(root, 'story_state.json')))
                requests = []
                second = iter([json.dumps(fixed, ensure_ascii=False),
                               json.dumps({'pass': True, 'reject': [], 'missing': []})])
                def review(_system, prompt, **_kwargs):
                    requests.append(prompt)
                    return next(second)
                app.call_llm_review = review
                prepared = app._prepare_structured_state_delta(
                    1, chapter, repair_context=contexts[-1])
        self.assertEqual(0, prepared['delta']['resources'][0]['quantity'])
        self.assertEqual(2, len(requests))  # Fresh extraction and full audit.
        self.assertIn('余额应为零', requests[0])
        self.assertIn('上次失败事务留下的待修复输出', requests[0])
        self.assertEqual('REJECTED', contexts[-1]['status'])

    def test_manual_state_repair_persists_only_rejected_hint_for_exact_context(self):
        app = self.make_app()
        app._append_batch_audit = lambda payload: None
        app._formal_suffix_preflight_fingerprint = mock.Mock(return_value='a' * 64)
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            args = app._manual_chapter_state_repair_arguments(75, '正文', '大纲')
            self.assertIsNone(args['repair_context'])
            hint = {'status': 'REJECTED', 'issues': ['引用太窄']}
            args['rejection_callback'](hint)
            with self.assertRaisesRegex(ValueError, 'REJECTED'):
                args['rejection_callback']({'status': 'PASS'})
            again = app._manual_chapter_state_repair_arguments(75, '正文', '大纲')
            self.assertEqual(hint, again['repair_context'])
            self.assertIsNone(gui_app.formal_suffix_rewrite.load_preflight_stage(
                root, chapter=75, context_fingerprint='a' * 64, stage='structured_state'))
            app._formal_suffix_preflight_fingerprint.return_value = 'b' * 64
            changed = app._manual_chapter_state_repair_arguments(75, '新正文', '大纲')
            self.assertIsNone(changed['repair_context'])
        app._formal_suffix_preflight_fingerprint.assert_called_with(75, '新正文', '大纲')

    def test_manual_state_repair_unavailable_stops_instead_of_fresh_extraction(self):
        app = self.make_app()
        app._append_batch_audit = mock.Mock()
        app._formal_suffix_preflight_fingerprint = mock.Mock(side_effect=OSError('读失败'))
        with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
            app._manual_chapter_state_repair_arguments(75, '正文', '大纲')
        self.assertEqual('manual_state_repair_cache_unavailable',
                         app._append_batch_audit.call_args.args[0]['event'])

    def test_manual_state_repair_keeps_formal_suffix_stage_binding(self):
        app = self.make_app()
        app._formal_suffix_state_repair_arguments = mock.Mock(return_value={'existing': True})
        self.assertEqual({'existing': True}, app._manual_chapter_state_repair_arguments(
            74, '正文', '大纲', 'f' * 64))
        app._formal_suffix_state_repair_arguments.assert_called_once_with(74, 'f' * 64)

    def test_state_audit_receives_verified_history_outside_summary_budget(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        old = '第1章 回做\n林舟左脚回做，接应人退了一步。'
        chapter = '第2章 杯子\n林舟放下水杯。'
        delta = {'chapter': 2, 'events': [{'event_type': 'world',
                 'summary': '林舟放下水杯', 'evidence_quote': '林舟放下水杯。'}]}
        state = gui_app.state_ledger.initial_state()
        state['current_chapter'] = 1
        state['applied_chapters']['1'] = gui_app.state_ledger.chapter_sha256(old)
        requests = []
        responses = iter([json.dumps(delta, ensure_ascii=False),
                          json.dumps({'pass': True, 'reject': [], 'missing': []})])
        def review(_system, prompt, **_kwargs):
            requests.append(prompt)
            return next(responses)
        app.call_llm_review = review
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, '第0001章.txt')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(old)
            app._official_chapter_paths_by_number = lambda: {1: path}
            with mock.patch.dict(generator.DIRS, {'plot': root}):
                app._prepare_structured_state_delta(2, chapter, state_override=state)
        self.assertEqual(2, len(requests))
        self.assertIn('林舟左脚回做，接应人退了一步。', requests[1])
        self.assertIn(state['applied_chapters']['1'], requests[1])

    def test_local_state_patch_still_requires_full_independent_audit(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '林舟上车。林舟收好钥匙。'
        delta = {'chapter': 1, 'characters': [{'name': '林舟', 'location': '回家路上的车内',
                 'evidence_quote': '林舟上车。'}],
                 'events': [{'event_type': 'world', 'summary': '林舟收好钥匙', 'evidence_quote': '林舟收好钥匙。'}]}
        reject = {'pass': False, 'reject': [{'path': 'characters[0]', 'reason': '地点仅支持车内',
                   'draft_quote': '林舟上车。', 'repair_instruction': '收窄location为车内'}]}
        app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(reject),
            json.dumps({'updates': {'characters[0]': {'location': '车内'}}}),
            json.dumps({'pass': True, 'reject': [], 'missing': []})])
        progress = []
        app._formal_suffix_rewrite_context = {'progress_callback': progress.append}
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(generator.DIRS, {'plot': root}):
            prepared = app._prepare_structured_state_delta(1, chapter)
            self.assertFalse(os.path.exists(os.path.join(root, 'story_state.json')))
        self.assertEqual('车内', prepared['delta']['characters'][0]['location'])
        self.assertEqual('林舟收好钥匙', prepared['delta']['events'][0]['summary'])
        self.assertEqual(4, app.call_llm_review.call_count)
        self.assertIn('只输出局部JSON补丁', app.call_llm_review.call_args_list[2].args[1])
        starts = [r for r in progress if r['event'] == 'state_model_call_started']
        self.assertEqual(['状态提取', '独立证据审计', '局部状态修复', '独立证据审计'], [r['phase'] for r in starts])
        self.assertTrue(all('elapsed_seconds' in r for r in progress if r['event'] == 'state_model_call_completed'))

    def test_repeated_state_rejections_persist_and_stop_next_invocation_before_model(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '林舟离开旧站。'
        delta = {'chapter': 1, 'events': [{'event_type': 'world', 'summary': '林舟离开旧站', 'evidence_quote': chapter}]}
        reject = {'pass': False, 'reject': [{'path': 'events[0]', 'reason': '摘要未通过', 'draft_quote': chapter}]}
        contexts = []
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(generator.DIRS, {'plot': root}):
            for _ in range(2):
                app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(reject)])
                with self.assertRaises(gui_app.state_ledger.StateExtractionError):
                    app._prepare_structured_state_delta(1, chapter,
                        repair_context=contexts[-1] if contexts else None, rejection_callback=contexts.append)
            app.call_llm_review = mock.Mock()
            with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STOPPED'):
                app._prepare_structured_state_delta(1, chapter, repair_context=contexts[-1])
            app.call_llm_review.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(root, 'story_state.json')))
        self.assertEqual(2, len(contexts[-1]['repair_history']))
        self.assertEqual('state_only', contexts[-1]['repair_scope'])

    def test_state_failure_preserves_candidate_across_restart_instead_of_rewriting(self):
        app = self.make_app()
        app._append_batch_audit = lambda payload: None
        pause, rewrite = mock.Mock(), mock.Mock()
        content = '第75章 候选\n\n林舟上车。'
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            path = os.path.join(root, '第0075章_待审.txt')
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(content)
            app._handle_state_preparation_failure(75, content, gui_app.state_ledger.StateExtractionError('状态失败'),
                                                 '状态失败', pause, rewrite)
            pause.assert_called_once_with('状态失败', content)
            rewrite.assert_not_called()
            resumed = self.make_app(model='another-model')
            resumed.project_dir = root
            self.assertEqual(content, resumed._load_state_only_candidate(75, path))
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(content + '新内容')
            self.assertIsNone(resumed._load_state_only_candidate(75, path))
            app._handle_state_preparation_failure(75, content, gui_app.state_ledger.StateProseConflictError('正文矛盾'),
                                                 '正文矛盾', pause, rewrite)
            rewrite.assert_called_once_with('正文矛盾', content)

    def test_unknown_state_error_and_failed_retry_marker_never_rewrite_prose(self):
        app = self.make_app()
        app._append_batch_audit = mock.Mock()
        app._remember_state_only_candidate = mock.Mock(side_effect=OSError('磁盘错误'))
        pause, rewrite = mock.Mock(), mock.Mock()
        app._handle_state_preparation_failure(1, '原文', RuntimeError('调用错误'), '调用错误', pause, rewrite)
        pause.assert_called_once_with('调用错误', '原文')
        rewrite.assert_not_called()
        self.assertEqual('state_candidate_marker_failed', app._append_batch_audit.call_args.args[0]['event'])

    def test_out_of_scope_patch_does_not_replace_saved_state_repair_base(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                            state_delta_independent_audit=True, temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '林舟上车。林舟收好钥匙。'
        delta = {'chapter': 1, 'characters': [{'name': '林舟', 'location': '回家路上的车内',
                 'evidence_quote': '林舟上车。'}],
                 'events': [{'event_type': 'world', 'summary': '林舟收好钥匙', 'evidence_quote': '林舟收好钥匙。'}]}
        reject = {'pass': False, 'reject': [{'path': 'characters[0]', 'reason': '地点范围超出证据',
                   'draft_quote': '林舟上车。'}]}
        contexts = []
        app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(reject),
                                                   json.dumps({'remove': ['events[0]']})])
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(generator.DIRS, {'plot': root}):
            with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, '禁止修改未被拒绝'):
                app._prepare_structured_state_delta(1, chapter, rejection_callback=contexts.append)
            self.assertFalse(os.path.exists(os.path.join(root, 'story_state.json')))
        # Validation may add empty categories before the first rejection. An
        # invalid patch must preserve that exact saved base, not the pre-normalized input.
        self.assertEqual(contexts[0]['previous_raw'], contexts[-1]['previous_raw'])
        self.assertEqual(delta['events'], json.loads(contexts[-1]['previous_raw'])['events'])
        self.assertIn('characters[0]', contexts[-1]['issues'][0])
        self.assertEqual(3, app.call_llm_review.call_count)

    def test_state_repair_context_for_other_body_is_ignored(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                            state_delta_independent_audit=False, pre_save_continuity_audit_enabled=False,
                            temporal_memory_enabled=False)
        app._append_batch_audit = lambda payload: None
        chapter = '第1章 旧站\n\n林舟离开旧站。'
        delta = {'chapter': 1, 'characters': [], 'resources': [], 'relationships': [],
                 'hooks': [], 'subplots': [], 'events': [{'event_type': 'world',
                 'summary': '林舟离开旧站', 'evidence_quote': '林舟离开旧站。'}]}
        requests = []
        app.call_llm_review = lambda _system, prompt, **_kwargs: (
            requests.append(prompt) or json.dumps(delta, ensure_ascii=False))
        stale = {'schema_version': 1, 'status': 'REJECTED', 'chapter': 1,
                 'chapter_sha256': '0' * 64, 'previous_raw': '{}', 'issues': ['旧错误']}
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(generator.DIRS, {'plot': root}):
                app._prepare_structured_state_delta(1, chapter, repair_context=stale)
        self.assertNotIn('旧错误', requests[0])

    def test_formal_state_repair_hook_uses_rejected_stage_only(self):
        app = self.make_app()
        app._durable_state_repair_arguments = mock.Mock(return_value={'repair_context': {'status': 'REJECTED'}})
        self.assertEqual({}, app._formal_suffix_state_repair_arguments(74, ''))
        args = app._formal_suffix_state_repair_arguments(74, 'f' * 64)
        app._durable_state_repair_arguments.assert_called_once_with(
            74, 'f' * 64, 'structured_state_rejected')
        self.assertEqual({'status': 'REJECTED'}, args['repair_context'])

    def test_structural_state_errors_can_be_repaired_then_fully_audited(self):
        chapter = '林舟离开旧站。'
        event = {'event_type': 'world', 'summary': '林舟离开旧站', 'evidence_quote': chapter}
        for initial, patch in (({'chapter': 2, 'events': [event]}, {'updates': {}}),
                               ({'chapter': 1, 'events': []}, {'append': {'events': [event]}}),
                               ({'events': [event]}, {'updates': {}})):
            app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                                temporal_memory_enabled=False)
            app._append_batch_audit = lambda row: None
            app.call_llm_review = mock.Mock(side_effect=[json.dumps(initial), json.dumps(patch),
                json.dumps({'pass': True, 'reject': [], 'missing': []})])
            with self.subTest(initial=initial), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(generator.DIRS, {'plot': root}):
                result = app._prepare_structured_state_delta(1, chapter)
                self.assertEqual(1, result['delta']['chapter'])
                self.assertEqual(1, len(result['delta']['events']))
                self.assertEqual(3, app.call_llm_review.call_count)
                self.assertFalse(os.path.exists(os.path.join(root, 'story_state.json')))

    def test_failed_rejection_store_stops_immediately_and_blocks_reentry(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=3,
                            temporal_memory_enabled=False)
        app._append_batch_audit = lambda row: None
        chapter = '林舟离开旧站。'
        delta = {'chapter': 1, 'events': [{'event_type': 'world', 'summary': '林舟离开旧站', 'evidence_quote': chapter}]}
        rejection = {'pass': False, 'reject': [{'path': 'events[0]', 'reason': '摘要待修', 'draft_quote': chapter}]}
        app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(rejection)])
        callback = mock.Mock(side_effect=OSError('disk full'))
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(generator.DIRS, {'plot': root}):
            for _ in range(7):
                with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                    app._prepare_structured_state_delta(1, chapter, rejection_callback=callback)
        self.assertEqual(2, app.call_llm_review.call_count)
        self.assertEqual(1, callback.call_count)

    def test_storage_probe_failure_blocks_fresh_apps_before_model_calls(self):
        with tempfile.TemporaryDirectory() as root:
            for _ in range(2):
                app = self.make_app()
                app.project_dir = root
                app.call_llm_review = mock.Mock()
                app._formal_suffix_preflight_fingerprint = lambda *args: 'f' * 64
                with mock.patch('gui_app.formal_suffix_rewrite.store_preflight_stage', side_effect=OSError('disk full')):
                    with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                        app._manual_chapter_state_repair_arguments(1, '正文', '大纲')
                app.call_llm_review.assert_not_called()
            # Recover only after a new successful durable probe.
            args = app._manual_chapter_state_repair_arguments(1, '正文', '大纲')
            self.assertEqual('', app._state_repair_persistence_fault)
            self.assertTrue(callable(args['rejection_callback']))

    def test_storage_readback_failure_blocks_even_when_write_returns_success(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            with mock.patch('gui_app.formal_suffix_rewrite.store_preflight_stage', return_value='ignored'):
                with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                    app._durable_state_repair_arguments(1, 'f' * 64, 'structured_state_rejected')

    def test_corrupt_history_is_not_silently_replaced_by_probe(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as root:
            app.project_dir = root
            args = app._durable_state_repair_arguments(1, 'f' * 64, 'structured_state_rejected')
            args['rejection_callback']({'status': 'REJECTED', 'issues': ['原反馈']})
            path = gui_app.formal_suffix_rewrite._preflight_cache_path(
                gui_app.formal_suffix_rewrite.Path(root), 1, 'f' * 64)
            path.write_text('{corrupt', encoding='utf-8')
            with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                app._durable_state_repair_arguments(1, 'f' * 64, 'structured_state_rejected')
            self.assertEqual('{corrupt', path.read_text(encoding='utf-8'))

    def test_unvalidated_draft_rejections_never_authorize_rewrite(self):
        chapter = '林舟离开旧站。'
        event = {'event_type': 'world', 'summary': '林舟离开旧站', 'evidence_quote': chapter}
        delta = {'chapter': 1, 'events': [event]}
        row = {'path': 'events[0]', 'reason': '摘要待修', 'draft_quote': chapter}
        bad = {'path': 'draft', 'reason': '无据正文矛盾', 'draft_quote': '正文里没有这句话'}
        good = {**bad, 'draft_quote': chapter}
        cases = [({'pass': False, 'reject': [row] * 40 + [bad]}, False),
                 ({'pass': False, 'reject': [row] * 40 + [good]}, False),
                 ({'pass': False, 'reject': [bad]}, False),
                 ({'pass': True, 'reject': [good]}, False),
                 ({'pass': False, 'reject': [good]}, True)]
        for audit, can_rewrite in cases:
            app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                                temporal_memory_enabled=False)
            app._append_batch_audit = lambda row: None
            app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(audit)])
            with self.subTest(audit=audit), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(generator.DIRS, {'plot': root}):
                app.project_dir = root
                with self.assertRaises(gui_app.state_ledger.StateLedgerError) as raised:
                    app._prepare_structured_state_delta(1, chapter)
                pause, rewrite = mock.Mock(), mock.Mock()
                app._handle_state_preparation_failure(1, chapter, raised.exception, str(raised.exception), pause, rewrite)
                self.assertEqual(int(can_rewrite), rewrite.call_count)
                self.assertEqual(int(not can_rewrite), pause.call_count)

    def test_empty_and_oversized_outputs_keep_failure_history_across_restarts(self):
        for raw in ('', 'x' * 60001):
            calls = []
            with self.subTest(length=len(raw)), tempfile.TemporaryDirectory() as root, \
                 mock.patch.dict(generator.DIRS, {'plot': root}):
                for _ in range(7):
                    app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=2,
                                        temporal_memory_enabled=False)
                    app.project_dir = root
                    app._append_batch_audit = lambda row: None
                    app._formal_suffix_preflight_fingerprint = lambda *args: 'f' * 64
                    app.call_llm_review = mock.Mock(return_value=raw)
                    args = app._manual_chapter_state_repair_arguments(1, '林舟离开旧站。', '')
                    with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STOPPED'):
                        app._prepare_structured_state_delta(1, '林舟离开旧站。', **args)
                    calls.append(app.call_llm_review.call_count)
                self.assertEqual([2, 0, 0, 0, 0, 0, 0], calls)
                self.assertEqual(2, len(args['repair_context']['repair_history']))
                self.assertEqual('', args['repair_context']['previous_raw'])
                self.assertEqual(bool(raw), args['repair_context']['previous_raw_omitted'])

    def test_probe_cannot_unlock_actual_failed_payload_and_recovery_preserves_history(self):
        app = self.make_app(structured_state_enabled=True, state_delta_max_attempts=1,
                            temporal_memory_enabled=False)
        app._append_batch_audit = lambda row: None
        app._formal_suffix_preflight_fingerprint = lambda *args: 'f' * 64
        chapter = '林舟离开旧站。'
        delta = {'chapter': 1, 'events': [{'event_type': 'world', 'summary': '林舟离开旧站', 'evidence_quote': chapter}]}
        audit = {'pass': False, 'reject': [{'path': 'events[0]', 'reason': '摘要待修', 'draft_quote': chapter}]}
        app.call_llm_review = mock.Mock(side_effect=[json.dumps(delta), json.dumps(audit)])
        real_store = gui_app.formal_suffix_rewrite.store_preflight_stage
        def selective_failure(*args, **kwargs):
            if kwargs['stage'] == 'manual_structured_state_rejected':
                raise OSError('only actual rejection write fails')
            return real_store(*args, **kwargs)
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(generator.DIRS, {'plot': root}):
            app.project_dir = root
            with mock.patch('gui_app.formal_suffix_rewrite.store_preflight_stage', side_effect=selective_failure):
                for _ in range(7):
                    with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                        args = app._manual_chapter_state_repair_arguments(1, chapter, '')
                        app._prepare_structured_state_delta(1, chapter, **args)
                self.assertEqual(2, app.call_llm_review.call_count)
                self.assertTrue(app._state_repair_pending_rejections)
                restarted = self.make_app()
                restarted.project_dir = root
                with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                    restarted._durable_state_repair_arguments(1, 'f' * 64, 'manual_structured_state_rejected')
            # Even a fully writable disk cannot substitute for lost failure data.
            with self.assertRaisesRegex(gui_app.state_ledger.StateExtractionError, 'STATE_REPAIR_STORAGE_FAILED'):
                restarted._durable_state_repair_arguments(1, 'f' * 64, 'manual_structured_state_rejected')
            recovered = app._manual_chapter_state_repair_arguments(1, chapter, '')
            self.assertEqual(1, len(recovered['repair_context']['repair_history']))
            self.assertIn('摘要待修', str(recovered['repair_context']['issues']))
            self.assertEqual('', app._state_repair_persistence_fault)
            self.assertFalse(app._state_repair_pending_rejections)
            # A fresh instance can now recover the actual persisted rejection too.
            again = restarted._durable_state_repair_arguments(1, 'f' * 64, 'manual_structured_state_rejected')
            self.assertEqual(recovered['repair_context'], again['repair_context'])

    def test_pending_marker_resolves_from_verified_record_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            app = self.make_app()
            app.project_dir = root
            args = app._durable_state_repair_arguments(1, 'f' * 64, 'structured_state_rejected')
            real_store = gui_app.formal_suffix_rewrite.store_preflight_stage
            def fail_resolution(*args, **kwargs):
                if kwargs['stage'].endswith('_pending') and kwargs['payload'].get('status') == 'RESOLVED':
                    raise OSError('interrupted after actual payload persisted')
                return real_store(*args, **kwargs)
            payload = {'status': 'REJECTED', 'issues': ['原反馈']}
            with mock.patch('gui_app.formal_suffix_rewrite.store_preflight_stage', side_effect=fail_resolution):
                with self.assertRaises(OSError):
                    args['rejection_callback'](payload)
            restarted = self.make_app()
            restarted.project_dir = root
            recovered = restarted._durable_state_repair_arguments(1, 'f' * 64, 'structured_state_rejected')
            self.assertEqual(payload, recovered['repair_context'])

    def test_normal_batch_state_progress_reaches_ui_and_audit_without_suffix(self):
        for fail in (False, True):
            app = self.make_app()
            app._ui_progress_append = mock.Mock()
            app._append_batch_audit = mock.Mock()
            app.call_llm_review = mock.Mock(side_effect=RuntimeError('offline')) if fail else mock.Mock(return_value='{}')
            try:
                app._call_state_review('s', 'p', chapter=1, attempt=2, phase='独立证据审计', temp=0)
            except RuntimeError:
                self.assertTrue(fail)
            rows = [call.args[0] for call in app._append_batch_audit.call_args_list]
            self.assertEqual(['state_model_call_started', 'state_model_call_failed' if fail else 'state_model_call_completed'],
                             [row['event'] for row in rows])
            self.assertIn('elapsed_seconds', rows[-1])
            messages = ''.join(call.args[0] for call in app._ui_progress_append.call_args_list)
            for expected in ('第1章', '第2轮', '独立证据审计', '耗时'):
                self.assertIn(expected, messages)

    def test_pre_save_continuity_rejection_never_creates_official_memory(self):
        app = self.make_app(
            structured_state_enabled=True,
            state_delta_max_attempts=2,
            state_delta_independent_audit=True,
            temporal_memory_enabled=True,
            subplot_stale_warn_chapters=8,
        )
        app._append_batch_audit = lambda payload: None
        chapter = "第1章 旧站\n\n林舟想起站长昨天已经把幕后真相全部告诉了他。"
        delta = {
            "chapter": 1,
            "characters": [{
                "name": "林舟", "location": "", "condition": "", "emotion": "",
                "status": "active", "knowledge_add": ["站长已经告知幕后真相"],
                "abilities_add": [],
                "evidence_quote": "林舟想起站长昨天已经把幕后真相全部告诉了他。",
            }],
            "resources": [], "relationships": [], "hooks": [], "subplots": [],
            "events": [{
                "event_type": "reveal", "summary": "林舟回忆站长已告知真相",
                "evidence_quote": "林舟想起站长昨天已经把幕后真相全部告诉了他。",
            }],
        }
        responses = iter([
            json.dumps(delta, ensure_ascii=False),
            json.dumps({
                "pass": False,
                "reject": [{
                    "path": "draft",
                    "reason": "已有正史没有这次告知过程，属于虚假回忆",
                    "draft_quote": "林舟想起站长昨天已经把幕后真相全部告诉了他。",
                    "state_reference": "林舟没有可验证的秘密知识",
                    "repair_instruction": "删除回忆，改为本章首次调查",
                }],
            }, ensure_ascii=False),
        ])
        app.call_llm_review = lambda *args, **kwargs: next(responses)
        with tempfile.TemporaryDirectory() as temp_dir:
            plot_dir = os.path.join(temp_dir, "plot")
            os.makedirs(plot_dir)
            with mock.patch.dict(generator.DIRS, {"plot": plot_dir}):
                with self.assertRaisesRegex(Exception, "虚假回忆"):
                    app._prepare_structured_state_delta(1, chapter, "林舟进入旧站")
            self.assertFalse(os.path.exists(os.path.join(plot_dir, "state_deltas")))
            self.assertFalse(os.path.exists(os.path.join(plot_dir, "story_state.json")))

    def test_existing_book_core_fields_are_locked(self):
        app = self.make_app(
            book_title="旧书",
            genre_template="科幻",
            book_brief="原主题",
            target_total_chapters=100,
            volume_ranges=[[1, 50, "第一卷"], [51, 100, "第二卷"]],
        )
        app._project_has_official_chapters = lambda folder=None: True
        with self.assertRaisesRegex(RuntimeError, "请点“新建一本”"):
            app._configure_book_project("新书名", "科幻", "原主题", 100, 50)

    def test_narrative_guard_requires_evidenced_pass(self):
        app = self.make_app(
            narrative_guard_enabled=True,
            narrative_guard_min_score=75,
            narrative_guard_drift_window=5,
            narrative_guard_marginal_score=82,
            narrative_guard_max_consecutive_marginal=2,
        )
        app._append_batch_audit = lambda payload: None
        app._commercial_contract_context = lambda max_chars=7000: "核心卖点：旧站求生"
        app.get_review_model_name = lambda: "deepseek-v4-pro"
        chapter = "第1章 断电\n\n林舟在旧站醒来。他关闭了旧站电闸，黑暗中传来脚步声。"
        outline = "第1章 断电\n核心事件：林舟关闭旧站电闸\n章末钩子：黑暗中传来脚步声"
        requirements = narrative_guard.extract_outline_requirements(outline)
        quotes = {
            "核心事件": "他关闭了旧站电闸",
            "章末钩子": "黑暗中传来脚步声",
        }
        payload = {
            "verdict": "PASS",
            "scores": {field: 90 for field in narrative_guard.SCORE_FIELDS},
            "outline_coverage": [
                {
                    "id": row["id"],
                    "status": "MET",
                    "draft_quote": quotes[row["label"]],
                    "reason": "正文已完成",
                }
                for row in requirements
            ],
            "mainline_progress": "林舟主动切断旧站电源并遭遇新威胁",
            "progress_evidence_quote": "他关闭了旧站电闸",
            "drift_flags": [],
            "nonsense_flags": [],
            "fail_reasons": [],
            "summary": "主线明确",
        }
        app.call_llm_review = lambda *args, **kwargs: json.dumps(
            payload, ensure_ascii=False
        )
        with tempfile.TemporaryDirectory() as root:
            plot = os.path.join(root, "plot")
            os.makedirs(plot)
            with mock.patch.dict(generator.DIRS, {"plot": plot}, clear=True):
                result = app._run_narrative_quality_guard(1, chapter, outline)
        self.assertEqual("PASS", result["verdict"])
        self.assertEqual(
            narrative_guard.chapter_sha256(chapter), result["chapter_sha256"]
        )

    def test_narrative_guard_rejects_empty_outline_contract_before_model_call(self):
        app = self.make_app(narrative_guard_enabled=True)
        app._commercial_contract_context = lambda max_chars=7000: "核心卖点"
        app.call_llm_review = mock.Mock(
            side_effect=AssertionError("空细纲不应消耗审稿调用")
        )
        chapter = "第1章 开门\n\n林舟推开旧站铁门。"
        with tempfile.TemporaryDirectory() as root:
            plot = os.path.join(root, "plot")
            os.makedirs(plot)
            with mock.patch.dict(generator.DIRS, {"plot": plot}, clear=True):
                with self.assertRaisesRegex(
                    narrative_guard.NarrativeGuardError, "空合同"
                ):
                    app._run_narrative_quality_guard(1, chapter, "第1章 开门")
        app.call_llm_review.assert_not_called()

    def test_semantic_arc_audit_is_fail_closed_when_unavailable_or_malformed(self):
        app = self.make_app(
            semantic_arc_audit_lookback=5,
            semantic_arc_audit_fulltext_chars=24000,
            semantic_arc_audit_fail_closed=True,
        )
        chapter = "第10章 夜行\n\n林舟进入旧站。"
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                def raise_unavailable(*args, **kwargs):
                    raise RuntimeError("network down")

                app.call_llm_review = raise_unavailable
                unavailable = app._run_semantic_arc_audit(chapter, 10)
                app.call_llm_review = lambda *args, **kwargs: "一切正常"
                malformed = app._run_semantic_arc_audit(chapter, 10)
        self.assertEqual(
            ("FAIL", "FAIL"),
            (unavailable["status"], unavailable["current"]),
        )
        self.assertEqual(
            ("FAIL", "FAIL"),
            (malformed["status"], malformed["current"]),
        )
        self.assertTrue(malformed["malformed"])

    def test_semantic_arc_accepts_json_without_weakening_failure_decisions(self):
        app = self.make_app(semantic_arc_audit_fail_closed=True)
        cases = [
            ('{"FINAL":"PASS","CURRENT":"PASS","REASON":"证据一致"}', "PASS", "PASS", False),
            ('```json\n{"FINAL":"WARN","CURRENT":"PASS","REASON":"仅历史问题"}\n```', "WARN", "PASS", False),
            ('{"FINAL":"FAIL","CURRENT":"FAIL","REASON":"当前矛盾"}', "FAIL", "FAIL", False),
            ('FINAL: PASS\nCURRENT: PASS\nREASON: 一致', "PASS", "PASS", False),
            ('FINAL: FAIL\nCURRENT: PASS\nREASON: 历史矛盾', "FAIL", "PASS", False),
            ('{"FINAL":"PASS"}', "FAIL", "FAIL", True),
            ('{"FINAL":"PASS","CURRENT":"PASSIVE"}', "FAIL", "FAIL", True),
            ('{"FINAL":"FAIL","FINAL":"PASS","CURRENT":"PASS"}', "FAIL", "FAIL", True),
            ('FINAL: PASS\nFINAL: FAIL\nCURRENT: PASS', "FAIL", "FAIL", True),
            ('FINAL: PASSIVE\nCURRENT: PASS', "FAIL", "FAIL", True),
        ]
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root, "out": root}, clear=True
        ):
            for raw, final, current, malformed in cases:
                with self.subTest(raw=raw):
                    app.call_llm_review = mock.Mock(return_value=raw)
                    result = app._run_semantic_arc_audit("第70章 正文", 70)
                    self.assertEqual((result["status"], result["current"]), (final, current))
                    self.assertEqual(bool(result.get("malformed")), malformed)

    def test_simple_mode_rhetorical_density_is_advisory_not_blocking(self):
        app = self.make_app(release_mode="simple")
        rhetorical = {
            "type": "叙述模板重复",
            "severity": "HIGH",
            "matched_chapters": [31, 40],
        }
        factual = {
            "type": "时间线回档",
            "severity": "HIGH",
            "curr_chap": 40,
        }
        self.assertTrue(app._cross_issue_touches_chapter(rhetorical, 40))
        self.assertFalse(app._cross_issue_is_blocking(rhetorical))
        self.assertTrue(app._cross_issue_is_blocking(factual))

    def test_strict_mode_keeps_rhetorical_density_blocking(self):
        app = self.make_app(release_mode="strict")
        rhetorical = {
            "type": "叙述模板重复",
            "severity": "HIGH",
            "matched_chapters": [31, 40],
        }
        self.assertTrue(app._cross_issue_is_blocking(rhetorical))

    def test_manual_commit_passes_narrative_audit_into_transaction(self):
        app = self.make_app(
            canon_guard_enabled=False,
            chapter_char_min=10,
            chapter_char_target_min=10,
            chapter_char_target_max=120,
        )
        app._extract_chapter_outline = lambda chapter: f"第{chapter}章 开门"
        app._begin_chapter_model_budget = lambda chapter: None
        app._verify_existing_guarded_audit_history = lambda: True
        app._formal_suffix_preflight_fingerprint = mock.Mock(return_value="c" * 64)
        app._prepare_structured_state_delta = mock.Mock(return_value={
            "chapter": 1,
            "chapter_sha256": "prepared",
            "already_committed": False,
            "delta": {},
        })
        app._recover_pending_chapter_commit = lambda: {"status": "RECOVERED"}
        app._run_semantic_consistency_guard = lambda *args, **kwargs: {
            "passed": True, "issues": []
        }
        app._run_release_guard = lambda *args, **kwargs: {
            "status": "PASS", "summary": "通过"
        }
        app.refresh_status = lambda: None
        app.refresh_reader_files_silent = lambda: None
        app.current_req = "手动正文"
        content = "第1章 开门\n\n林舟推开旧站铁门，核对门锁后把现场编号写进记录簿。"
        audit = valid_automatic_audit(content)
        app._run_narrative_quality_guard = lambda *args, **kwargs: audit
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "plot": os.path.join(root, "plot"),
                "hist": os.path.join(root, "history"),
            }
            for path in dirs.values():
                os.makedirs(path)
            write_semantic_invariants(dirs["plot"])
            app.project_dir = root
            chapter_path = os.path.join(root, "output", "第0001章.txt")
            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch("gui_app.state_ledger.load_state", return_value={
                     "current_chapter": 0,
                     "applied_chapters": {},
                 }), \
                 mock.patch("gui_app.chapter_commit.begin") as begin, \
                 mock.patch("gui_app.chapter_commit.mark_step"), \
                 mock.patch(
                     "gui_app.continuity_guard.validate_chapter",
                     return_value={"status": "PASS", "summary": "通过"},
                 ):
                app._commit_manual_chapter(1, chapter_path, content, "手动新章")
        self.assertIs(audit, begin.call_args.kwargs["narrative_audit_result"])
        self.assertTrue(begin.call_args.kwargs["validation_receipt"])
        self.assertEqual(10, begin.call_args.kwargs["char_limits"]["min"])
        self.assertIsNone(app._prepare_structured_state_delta.call_args.kwargs["repair_context"])
        self.assertTrue(callable(
            app._prepare_structured_state_delta.call_args.kwargs["rejection_callback"]))

    def test_formal_suffix_commit_reuses_validated_preflight_cache(self):
        app = self.make_app(
            release_mode="strict",
            commercial_review_enabled=False,
            canon_guard_enabled=False,
            chapter_char_min=10,
            chapter_char_target_min=10,
            chapter_char_target_max=120,
        )
        app._formal_suffix_rewrite_context = {
            "cache_enabled": True,
            "progress_callback": lambda _row: None,
        }
        app._extract_chapter_outline = lambda chapter: f"第{chapter}章 开门"
        app._begin_chapter_model_budget = lambda chapter: None
        app._verify_existing_guarded_audit_history = lambda: True
        app._formal_suffix_preflight_fingerprint = lambda *args: "f" * 64
        app._recover_pending_chapter_commit = lambda: {"status": "RECOVERED"}
        app._run_semantic_consistency_guard = lambda *args, **kwargs: {
            "passed": True, "issues": []
        }
        app._run_release_guard = mock.Mock(
            side_effect=AssertionError("缓存命中后不应重跑发布设定总校")
        )
        app.refresh_status = lambda: None
        app.refresh_reader_files_silent = lambda: None
        app.current_req = "尾段重写"
        content = "第1章 开门\n\n林舟推开旧站铁门，核对门锁后把现场编号写进记录簿。"
        audit = valid_automatic_audit(content)
        prepared = {
            "chapter": 1,
            "chapter_sha256": narrative_guard.chapter_sha256(content),
            "already_committed": False,
            "delta": {},
        }
        release_guard = {
            "chapter": 1,
            "chapter_sha256": narrative_guard.chapter_sha256(content),
            "status": "PASS",
            "summary": "通过",
        }
        app._load_formal_suffix_preflight_stage = mock.Mock(
            side_effect=lambda _chapter, _fingerprint, stage: (
                release_guard
                if stage == "release_guard"
                else audit
                if stage == "narrative_audit"
                else prepared
            )
        )
        app._validate_cached_release_guard_preflight = mock.Mock(
            return_value=release_guard
        )
        app._validate_cached_narrative_preflight = mock.Mock(return_value=audit)
        app._validate_cached_state_preflight = mock.Mock(return_value=prepared)
        app._run_narrative_quality_guard = mock.Mock(
            side_effect=AssertionError("缓存命中后不应重跑叙事模型")
        )
        app._prepare_structured_state_delta = mock.Mock(
            side_effect=AssertionError("缓存命中后不应重跑状态模型")
        )
        app._store_formal_suffix_preflight_stage = mock.Mock()
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "plot": os.path.join(root, "plot"),
                "hist": os.path.join(root, "history"),
                "out": os.path.join(root, "output"),
            }
            for path in dirs.values():
                os.makedirs(path)
            write_semantic_invariants(dirs["plot"])
            app.project_dir = root
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch("gui_app.state_ledger.load_state", return_value={
                     "current_chapter": 0,
                     "applied_chapters": {},
                 }), \
                 mock.patch("gui_app.chapter_commit.begin") as begin, \
                 mock.patch("gui_app.chapter_commit.mark_step"), \
                 mock.patch(
                     "gui_app.continuity_guard.validate_chapter",
                     return_value={"status": "PASS", "summary": "通过"},
                 ):
                app._commit_manual_chapter(1, chapter_path, content, "尾段重写")

        self.assertIs(audit, begin.call_args.kwargs["narrative_audit_result"])
        self.assertIs(prepared, begin.call_args.kwargs["prepared_state_delta"])
        app._run_narrative_quality_guard.assert_not_called()
        app._prepare_structured_state_delta.assert_not_called()
        app._run_release_guard.assert_not_called()
        app._store_formal_suffix_preflight_stage.assert_not_called()

    def test_formal_suffix_caches_failed_commercial_review_before_rollback(self):
        app = self.make_app(
            release_mode="strict",
            commercial_review_enabled=True,
            commercial_review_milestones=[1],
            commercial_review_interval=0,
            commercial_review_volume_end=False,
            commercial_review_pause_on_fail=True,
            canon_guard_enabled=False,
            chapter_char_min=10,
            chapter_char_target_min=10,
            chapter_char_target_max=120,
        )
        app._formal_suffix_rewrite_context = {
            "cache_enabled": True,
            "progress_callback": lambda _row: None,
        }
        app._extract_chapter_outline = lambda chapter: f"第{chapter}章 开门"
        app._begin_chapter_model_budget = lambda chapter: None
        app._verify_existing_guarded_audit_history = lambda: True
        app._formal_suffix_preflight_fingerprint = lambda *args: "f" * 64
        app._load_formal_suffix_preflight_stage = mock.Mock(return_value=None)
        app._recover_pending_chapter_commit = lambda: {"status": "RECOVERED"}
        app._run_semantic_consistency_guard = lambda *args, **kwargs: {
            "passed": True, "issues": []
        }
        app._run_release_guard = lambda *args, **kwargs: {
            "status": "PASS", "summary": "通过"
        }
        app.refresh_status = lambda: None
        app.refresh_reader_files_silent = lambda: None
        app.current_req = "尾段重写"
        app._get_volume_ranges = lambda: []
        content = "第1章 开门\n\n林舟推开旧站铁门，核对门锁后把现场编号写进记录簿。"
        audit = valid_automatic_audit(content)
        app._run_narrative_quality_guard = lambda *args, **kwargs: audit
        app._prepare_structured_state_delta = lambda *args, **kwargs: {
            "chapter": 1,
            "chapter_sha256": narrative_guard.chapter_sha256(content),
            "already_committed": False,
            "delta": {},
        }
        review = {
            "status": "FAIL",
            "current": "PASS",
            "action": "PAUSE",
            "summary": "商业节奏未通过",
            "review_model": "test-model",
            "review_receipt": {"receipt_sha256": "signed"},
        }
        app._run_commercial_stage_review = mock.Mock(return_value=review)
        app._store_formal_suffix_preflight_stage = mock.Mock()

        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "plot": os.path.join(root, "plot"),
                "hist": os.path.join(root, "history"),
                "out": os.path.join(root, "output"),
            }
            for path in dirs.values():
                os.makedirs(path)
            write_semantic_invariants(dirs["plot"])
            app.project_dir = root
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch("gui_app.state_ledger.load_state", return_value={
                     "current_chapter": 0,
                     "applied_chapters": {},
                 }), \
                 mock.patch(
                     "gui_app.continuity_guard.validate_chapter",
                     return_value={"status": "PASS", "summary": "通过"},
                 ), \
                 mock.patch(
                     "gui_app.commercial_reviewer.load_revision_debt",
                     return_value={"schema_version": 2, "items": [], "review_history": []},
                 ), \
                 mock.patch(
                     "gui_app.commercial_reviewer.hard_gate_debt_blocked",
                     return_value=False,
                 ), \
                 mock.patch(
                     "gui_app.commercial_reviewer.verify_review_receipt"
                 ), \
                 self.assertRaisesRegex(RuntimeError, "商业节奏未通过"):
                app._commit_manual_chapter(1, chapter_path, content, "尾段重写")

        commercial_calls = [
            call for call in app._store_formal_suffix_preflight_stage.call_args_list
            if call.args[2] == "commercial_review"
        ]
        rejected_calls = [
            call for call in app._store_formal_suffix_preflight_stage.call_args_list
            if call.args[2] == "commercial_review_rejected"
        ]
        self.assertEqual(1, len(commercial_calls))
        self.assertIs(review, commercial_calls[0].args[3])
        self.assertEqual(1, len(rejected_calls))
        self.assertIs(review, rejected_calls[0].args[3]["review_result"])
        self.assertEqual("review_pause", rejected_calls[0].args[3]["reason"])
        self.assertFalse(review["gate_blocked"])

    def test_micro_edit_reuses_hash_bound_audit_and_valid_old_state(self):
        app = self.make_app(release_mode="strict")
        content = "第1章 开门\n\n林舟推开旧站铁门。"
        audit = {"chapter": 1, "chapter_sha256": narrative_guard.chapter_sha256(content)}
        old_delta = {"chapter": 1, "events": []}
        events = []
        app._formal_suffix_rewrite_context = {
            "micro_edit": True,
            "micro_unchanged_chapters": [1],
            "reusable_narrative_audits": {1: audit},
            "reusable_state_deltas": {1: old_delta},
            "progress_callback": events.append,
            "cache_enabled": True,
        }
        app._validate_cached_narrative_preflight = mock.Mock(return_value=audit)

        reused_audit = app._reuse_micro_edit_narrative_audit(1, content)
        release = app._reuse_micro_edit_release_guard(1, content)
        with mock.patch(
            "gui_app.state_ledger.validate_delta",
            return_value=(old_delta, []),
        ):
            prepared = app._reuse_micro_edit_state_delta(
                1,
                content,
                {"current_chapter": 0, "applied_chapters": {}},
            )

        self.assertIs(audit, reused_audit)
        self.assertEqual("PASS", release["status"])
        self.assertEqual(old_delta, prepared["delta"])
        self.assertTrue(
            any(row.get("event") == "preflight_evidence_reused" for row in events)
        )

    def test_micro_edit_state_reuse_prunes_only_unlocatable_old_evidence(self):
        app = self.make_app(release_mode="strict")
        content = "第1章 开门\n\n林舟推开旧站铁门。"
        old_delta = {"chapter": 1, "events": [{"evidence_quote": "已删除句"}]}
        pruned = {"chapter": 1, "events": []}
        app._formal_suffix_rewrite_context = {
            "micro_edit": True,
            "reusable_state_deltas": {1: old_delta},
            "progress_callback": lambda _row: None,
            "cache_enabled": True,
        }
        with mock.patch(
            "gui_app.state_ledger.validate_delta",
            side_effect=[({}, ["没有可在正文定位"]), (pruned, [])],
        ), mock.patch(
            "gui_app.state_ledger.prune_unlocatable_evidence_items",
            return_value=(pruned, ["events[0]"]),
        ):
            prepared = app._reuse_micro_edit_state_delta(
                1,
                content,
                {"current_chapter": 0, "applied_chapters": {}},
            )
        self.assertEqual(pruned, prepared["delta"])

    def test_simple_manual_commit_skips_per_chapter_model_and_state_audits(self):
        app = self.make_app(
            release_mode="simple",
            commercial_review_enabled=False,
            canon_guard_enabled=False,
            chapter_char_min=10,
            chapter_char_target_min=10,
            chapter_char_target_max=120,
        )
        app._extract_chapter_outline = lambda chapter: f"第{chapter}章 开门"
        app._begin_chapter_model_budget = lambda chapter: None
        app._run_narrative_quality_guard = mock.Mock(
            side_effect=AssertionError("简单模式不应逐章调用模型审稿")
        )
        app._prepare_structured_state_delta = mock.Mock(
            side_effect=AssertionError("简单模式不应提取结构化状态")
        )
        app._recover_pending_chapter_commit = lambda: {"status": "RECOVERED"}
        app._run_semantic_consistency_guard = lambda *args, **kwargs: {
            "passed": True, "issues": []
        }
        app._run_release_guard = lambda *args, **kwargs: {
            "status": "PASS", "summary": "通过"
        }
        app.refresh_status = lambda: None
        app.refresh_reader_files_silent = lambda: None
        app.current_req = "手动正文"
        content = "第1章 开门\n\n林舟推开旧站铁门，核对门锁后把现场编号写进记录簿。"
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "plot": os.path.join(root, "plot"),
                "hist": os.path.join(root, "history"),
                "out": os.path.join(root, "output"),
            }
            for path in dirs.values():
                os.makedirs(path)
            write_semantic_invariants(dirs["plot"])
            app.project_dir = root
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch("gui_app.chapter_commit.begin") as begin, \
                 mock.patch("gui_app.chapter_commit.mark_step"), \
                 mock.patch(
                     "gui_app.continuity_guard.validate_chapter",
                     return_value={"status": "PASS", "summary": "通过"},
                 ):
                app._commit_manual_chapter(1, chapter_path, content, "手动新章")
        self.assertIsNone(begin.call_args.kwargs["narrative_audit_result"])
        self.assertFalse(begin.call_args.kwargs["narrative_audit_required"])
        self.assertIsNone(begin.call_args.kwargs["prepared_state_delta"])
        app._run_narrative_quality_guard.assert_not_called()
        app._prepare_structured_state_delta.assert_not_called()

    def test_manual_save_warn_has_no_force_save_override(self):
        app = self.make_app()
        app.result_text = mock.Mock()
        app.result_text.get.return_value = "第1章 开门\n\n林舟推开旧站铁门。"
        app.next_chap = 1
        app.filepath = "unused.txt"
        app._run_release_guard = lambda *args, **kwargs: {
            "status": "WARN",
            "summary": "人物状态存在疑点",
            "raw": "",
        }
        app._commit_manual_chapter = mock.Mock(
            side_effect=AssertionError("WARN 不得进入正式保存")
        )
        with mock.patch("gui_app.messagebox.showerror") as showerror, \
             mock.patch("gui_app.messagebox.askyesno") as askyesno:
            app.save_new_chapter()
        self.assertTrue(showerror.called)
        askyesno.assert_not_called()
        app._commit_manual_chapter.assert_not_called()

    def test_manual_replacement_with_bad_old_audit_still_requires_new_review(self):
        app = self.make_app(
            canon_guard_enabled=False,
            narrative_guard_min_score=75,
            chapter_char_min=10,
            chapter_char_target_min=10,
            chapter_char_target_max=120,
        )
        app._extract_chapter_outline = lambda chapter: f"第{chapter}章 改写"
        app._begin_chapter_model_budget = lambda chapter: None
        app._verify_existing_guarded_audit_history = lambda: True
        app._load_latest_chapter_statuses = lambda: {}
        app._append_batch_audit = lambda *args, **kwargs: None
        app._run_semantic_consistency_guard = lambda *args, **kwargs: {
            "passed": True, "issues": []
        }
        app._run_release_guard = lambda *args, **kwargs: {
            "status": "PASS", "summary": "通过"
        }
        app._run_narrative_quality_guard = mock.Mock(
            side_effect=RuntimeError("新稿仍须重新自动审查")
        )
        old_text = "第1章 旧稿\n\n林舟进入旧站，检查门锁并记下现场编号。"
        new_text = "第1章 新稿\n\n林舟离开旧站，封存钥匙并写下新的现场编号。"
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "plot": os.path.join(root, "plot"),
                "hist": os.path.join(root, "history"),
                "logs": os.path.join(root, "logs"),
                "out": os.path.join(root, "output"),
            }
            for path in dirs.values():
                os.makedirs(path)
            write_semantic_invariants(dirs["plot"])
            app.project_dir = root
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(old_text)
            current_state = {
                "current_chapter": 1,
                "applied_chapters": {
                    "1": narrative_guard.chapter_sha256(old_text)
                },
            }
            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch(
                     "gui_app.state_ledger.load_state", return_value=current_state
                 ):
                with mock.patch(
                    "gui_app.continuity_guard.validate_chapter",
                    return_value={"status": "PASS", "summary": "通过"},
                ), self.assertRaisesRegex(RuntimeError, "新稿仍须重新自动审查"):
                    app._commit_manual_chapter(
                        1, chapter_path, new_text, "手动重写"
                    )
        app._run_narrative_quality_guard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
