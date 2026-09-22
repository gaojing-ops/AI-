import builtins
import json
import os
import tempfile
import unittest
from unittest import mock

import generator
import gui_app
import narrative_guard
import publishing_rules
import state_ledger


class PublishingRulesTests(unittest.TestCase):
    @staticmethod
    def _valid_narrative_audit(chapter_text):
        quote = chapter_text.split("\n\n", 1)[-1]
        raw_payload = {
            "verdict": "PASS",
            "scores": {
                field: 90 for field in narrative_guard.SCORE_FIELDS
            },
            "outline_coverage": [],
            "mainline_progress": "主角完成本章行动",
            "progress_evidence_quote": quote,
            "drift_flags": [],
            "nonsense_flags": [],
            "fail_reasons": [],
            "summary": "通过",
        }
        raw_response = json.dumps(raw_payload, ensure_ascii=False)
        audit, _issues = narrative_guard.validate_audit(
            raw_payload,
            chapter_text=chapter_text,
            requirements=[],
        )
        audit.update({
            "chapter": 1,
            "chapter_sha256": state_ledger.chapter_sha256(chapter_text),
            "review_model": "deepseek-v4-pro",
            "review_raw_response": raw_response,
        })
        audit["review_receipt"] = narrative_guard.build_review_receipt(
            chapter_text=chapter_text,
            system_prompt="test-system",
            user_prompt="test-user",
            raw_response=raw_response,
            review_model="deepseek-v4-pro",
            decision_payload=audit,
        )
        return audit

    def test_default_rules_are_fanqie_and_generic(self):
        rules = publishing_rules.default_tool_rules()
        self.assertEqual("番茄小说", rules["ruleset_metadata"]["platform"])
        self.assertTrue(rules["platform_semantic_rules"])
        self.assertEqual({}, rules["theme_contract"])
        self.assertEqual([], rules["chapter_gated_terms"])

    def test_theme_and_reveal_rules_are_derived_per_project(self):
        rules = publishing_rules.build_project_tool_rules(
            {
                "book_title": "测试书",
                "genre_template": "现实悬疑",
                "book_brief": "记者追查旧案。禁止：系统升级、修仙境界；避免无意义副本。",
            },
            {
                "topics": [
                    {
                        "label": "旧案真相",
                        "earliest_hard": 80,
                        "hard_patterns": ["市长是真凶"],
                    }
                ]
            },
        )
        self.assertEqual("测试书", rules["theme_contract"]["book_title"])
        self.assertIn("系统升级", rules["theme_avoid_terms"])
        self.assertIn("修仙境界", rules["theme_avoid_terms"])
        self.assertEqual(80, rules["chapter_gated_terms"][0][2])

    def test_theme_avoid_terms_accept_directive_without_colon(self):
        terms = publishing_rules.extract_theme_avoid_terms(
            "职业探案推进。禁止万能系统、无代价复制、后宫和靠新设定临时解题。"
        )
        self.assertEqual(
            ["万能系统", "无代价复制", "后宫", "靠新设定临时解题"],
            terms,
        )

    def test_project_rule_fingerprint_changes_with_reveal_sources(self):
        config = {
            "book_title": "测试书",
            "genre_template": "现实悬疑",
            "book_brief": "禁止万能系统。",
        }
        first = publishing_rules.project_rules_fingerprint(
            config, {"topics": [{"label": "旧案", "earliest_hard": 80}]}
        )
        second = publishing_rules.project_rules_fingerprint(
            config, {"topics": [{"label": "旧案", "earliest_hard": 90}]}
        )
        self.assertNotEqual(first, second)
        rules = publishing_rules.build_project_tool_rules(config, {"topics": []})
        self.assertEqual(
            publishing_rules.project_rules_fingerprint(config, {"topics": []}),
            rules["ruleset_metadata"]["project_fingerprint"],
        )

    def test_real_people_policy_is_rendered_and_changes_rules_fingerprint(self):
        config = {
            "book_title": "测试书",
            "genre_template": "体育竞技",
            "book_brief": "原创球员成长。",
            "real_people_policy": "不得以谐音、绰号或履历拼接影射现实球员。",
        }
        rules = publishing_rules.build_project_tool_rules(config, {"topics": []})
        rendered = publishing_rules.render_rules_document(rules)
        self.assertEqual(
            config["real_people_policy"],
            rules["theme_contract"]["real_people_policy"],
        )
        self.assertIn(config["real_people_policy"], rendered)
        different = dict(config, real_people_policy="所有角色均原创。")
        self.assertNotEqual(
            publishing_rules.project_rules_fingerprint(config, {"topics": []}),
            publishing_rules.project_rules_fingerprint(different, {"topics": []}),
        )

    def test_gui_detects_stale_generated_project_rules(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "tool_rules.json")
            config = {
                "book_title": "测试书",
                "genre_template": "现实悬疑",
                "book_brief": "禁止万能系统。",
            }
            reveal = {"topics": []}
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = config
            app._tool_rules_path = lambda: path
            app._load_reveal_rules = lambda: reveal
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(publishing_rules.default_tool_rules(), handle, ensure_ascii=False)
            synced, _message = app._project_rules_sync_status()
            self.assertFalse(synced)

            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    publishing_rules.build_project_tool_rules(config, reveal),
                    handle,
                    ensure_ascii=False,
                )
            synced, _message = app._project_rules_sync_status()
            self.assertTrue(synced)

    def test_gui_drift_check_accepts_structured_keywords(self):
        dummy = object.__new__(gui_app.NovelGeneratorGUI)
        dummy.BANNED_KEYWORDS = set()
        dummy._load_tool_rules = lambda: {
            "banned_keywords": [
                {"keyword": "站外导流", "reason": "测试"},
                ["重复广告", "测试"],
                "极端血腥",
            ]
        }
        hits = gui_app.NovelGeneratorGUI._check_content_drift(
            dummy,
            "正文里不应出现站外导流，也不应出现重复广告。",
        )
        self.assertEqual(["站外导流", "重复广告"], hits)

    def test_gui_drift_check_blocks_stage_gated_terms(self):
        dummy = object.__new__(gui_app.NovelGeneratorGUI)
        dummy.BANNED_KEYWORDS = set()
        dummy._load_tool_rules = lambda: {"banned_keywords": []}
        with mock.patch(
            "story_architect.blocked_terms_for_chapter",
            return_value=["B-17"],
        ):
            hits = gui_app.NovelGeneratorGUI._check_content_drift(
                dummy,
                "碎屑上提前出现B-17编号。",
                chap_num=1,
            )
        self.assertEqual(["B-17"], hits)

    def test_simple_mode_unbound_commercial_actions_are_persisted(self):
        app = object.__new__(gui_app.NovelGeneratorGUI)
        app._persist_commercial_review_result = mock.Mock()
        app._append_batch_audit = mock.Mock()
        result = {
            "status": "WARN",
            "action": "ADJUST",
            "severity": "REVISION",
        }
        persisted = app._persist_unbound_commercial_review_actions(
            result, 40, bound_to_commit=False
        )
        self.assertTrue(persisted)
        app._persist_commercial_review_result.assert_called_once_with(result, 40)
        self.assertEqual(
            "commercial_revision_actions_persisted",
            app._append_batch_audit.call_args.args[0]["event"],
        )

        app._persist_commercial_review_result.reset_mock()
        self.assertFalse(app._persist_unbound_commercial_review_actions(
            result, 40, bound_to_commit=True
        ))
        app._persist_commercial_review_result.assert_not_called()

    def test_status_write_failure_is_a_hard_error(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            logs_dir = os.path.join(root, "logs")
            os.makedirs(out_dir)
            chapter_path = os.path.join(out_dir, "第1章.txt")
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write("第1章\n\n正文")
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app._append_batch_audit = lambda *args, **kwargs: None
            real_open = builtins.open

            def failing_open(path, mode="r", *args, **kwargs):
                if os.path.basename(str(path)) == "chapter_status.jsonl" and "a" in mode:
                    raise OSError("disk full")
                return real_open(path, mode, *args, **kwargs)

            with mock.patch.dict(
                generator.DIRS,
                {"out": out_dir, "logs": logs_dir},
                clear=True,
            ), mock.patch("builtins.open", side_effect=failing_open):
                with self.assertRaisesRegex(RuntimeError, "禁止继续发布"):
                    app._record_chapter_status(
                        1, "正式可用", [], chapter_path, 2
                    )

    def test_publish_sync_rejects_missing_or_stale_status_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "output")
            publish_dir = os.path.join(root, "publish")
            logs_dir = os.path.join(root, "logs")
            for path in (out_dir, publish_dir, logs_dir):
                os.makedirs(path)
            chapter_path = os.path.join(out_dir, "第1章.txt")
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write("第1章\n\n当前正文")
            app = object.__new__(gui_app.NovelGeneratorGUI)
            with mock.patch.dict(
                generator.DIRS,
                {"out": out_dir, "publish": publish_dir, "logs": logs_dir},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "缺少可核验"):
                    app._sync_approved_chapters_to_publish(1)
                with open(
                    os.path.join(logs_dir, "chapter_status.jsonl"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write('{"chapter":1,"status":"正式可用","chapter_sha256":"old"}\n')
                with self.assertRaisesRegex(RuntimeError, "哈希不一致"):
                    app._sync_approved_chapters_to_publish(1)

    def test_publish_requires_matching_persisted_narrative_audit(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "publish": os.path.join(root, "publish"),
                "logs": os.path.join(root, "logs"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            chapter_path = os.path.join(dirs["out"], "第1章.txt")
            chapter_text = "第1章 开门\n\n林舟推开旧站铁门。"
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(chapter_text)
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {"narrative_guard_min_score": 75}
            app._append_batch_audit = lambda *args, **kwargs: None
            audit = self._valid_narrative_audit(chapter_text)
            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                with open(
                    os.path.join(dirs["logs"], "chapter_status.jsonl"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(
                        '{"chapter":1,"status":"正式可用",'
                        f'"chapter_sha256":"{state_ledger.chapter_sha256(chapter_text)}"}}\n'
                    )
                with self.assertRaisesRegex(
                    narrative_guard.NarrativeGuardError, "缺少主线与可读性"
                ):
                    app._sync_approved_chapters_to_publish(1)

                narrative_guard.persist_audit(
                    dirs["plot"],
                    1,
                    chapter_text,
                    audit,
                    model_name="deepseek-v4-pro",
                )
                os.remove(os.path.join(dirs["logs"], "chapter_status.jsonl"))
                app._record_chapter_status(
                    1,
                    "正式可用",
                    [],
                    chapter_path,
                    10,
                    narrative_audit_result=audit,
                )
                status = app._load_latest_chapter_statuses()[1]
                self.assertEqual(
                    "deepseek-v4-pro", status["narrative_review_model"]
                )
                app.config["review_model"] = "future-review-model"
                self.assertEqual(1, app._sync_approved_chapters_to_publish(1))
                self.assertTrue(os.path.exists(
                    os.path.join(dirs["publish"], "第0001章.txt")
                ))

    def test_publish_sync_preserves_matching_legacy_publish_mirror(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "publish": os.path.join(root, "publish"),
                "logs": os.path.join(root, "logs"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            chapter_text = "第1章 旧稿\n\n这章已经发布并封存。\n"
            chapter_path = os.path.join(dirs["out"], "第1章.txt")
            publish_path = os.path.join(dirs["publish"], "第0001章.txt")
            for path in (chapter_path, publish_path):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(chapter_text)
            with open(
                os.path.join(dirs["logs"], "chapter_status.jsonl"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(
                    '{"chapter":1,"status":"正式可用",'
                    f'"chapter_sha256":"{state_ledger.chapter_sha256(chapter_text)}"}}\n'
                )
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {"narrative_guard_min_score": 75}
            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                self.assertEqual(0, app._sync_approved_chapters_to_publish(1))
                self.assertEqual(
                    chapter_text,
                    generator.read_text_exact(publish_path),
                )

    def test_legacy_status_uses_persisted_audit_model_before_current_config(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "publish": os.path.join(root, "publish"),
                "logs": os.path.join(root, "logs"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            chapter_path = os.path.join(dirs["out"], "第1章.txt")
            chapter_text = "第1章 开门\n\n林舟推开旧站铁门。"
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(chapter_text)
            audit = self._valid_narrative_audit(chapter_text)
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {
                "narrative_guard_min_score": 75,
                "review_model": "future-review-model",
            }
            audit_id = narrative_guard.calculate_audit_id(audit, 1, chapter_text)
            status = {
                "chapter": 1,
                "status": "正式可用",
                "chapter_sha256": state_ledger.chapter_sha256(chapter_text),
                "narrative_audit_id": audit_id,
            }
            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                narrative_guard.persist_audit(
                    dirs["plot"], 1, chapter_text, audit,
                    model_name="deepseek-v4-pro",
                )
                with open(
                    os.path.join(dirs["logs"], "chapter_status.jsonl"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(json.dumps(status, ensure_ascii=False) + "\n")
                published = app._copy_to_publish(chapter_path, 1)
            self.assertTrue(os.path.exists(published))

    def test_recorded_empty_narrative_model_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "publish": os.path.join(root, "publish"),
                "logs": os.path.join(root, "logs"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            chapter_path = os.path.join(dirs["out"], "第1章.txt")
            chapter_text = "第1章 开门\n\n林舟推开旧站铁门。"
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(chapter_text)
            audit = self._valid_narrative_audit(chapter_text)
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {
                "narrative_guard_min_score": 75,
                "review_model": "future-review-model",
            }
            status = {
                "chapter": 1,
                "status": "正式可用",
                "chapter_sha256": state_ledger.chapter_sha256(chapter_text),
                "narrative_audit_id": narrative_guard.calculate_audit_id(
                    audit, 1, chapter_text
                ),
                "narrative_review_model": "",
            }
            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                narrative_guard.persist_audit(
                    dirs["plot"], 1, chapter_text, audit,
                    model_name="deepseek-v4-pro",
                )
                with open(
                    os.path.join(dirs["logs"], "chapter_status.jsonl"),
                    "w",
                    encoding="utf-8",
                ) as handle:
                    handle.write(json.dumps(status, ensure_ascii=False) + "\n")
                with self.assertRaisesRegex(
                    narrative_guard.NarrativeGuardError,
                    "状态记录缺少有效的自动审查模型",
                ):
                    app._copy_to_publish(chapter_path, 1)

    def test_persistence_uses_models_sealed_in_review_results(self):
        chapter_text = "第1章 开门\n\n林舟推开旧站铁门。"
        audit = self._valid_narrative_audit(chapter_text)
        app = object.__new__(gui_app.NovelGeneratorGUI)
        app.config = {
            "narrative_guard_min_score": 75,
            "review_model": "future-review-model",
        }
        app._persist_commercial_review_actions = lambda *args, **kwargs: None
        commercial_result = {
            "review_receipt": {"model": "historical-commercial-model"}
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            generator.DIRS, {"plot": root}, clear=True
        ), mock.patch.object(
            app, "_commercial_review_report_dir", return_value=root
        ), mock.patch(
            "gui_app.commercial_reviewer.persist_review"
        ) as persist_commercial:
            app._persist_narrative_audit_result(audit, 1, chapter_text)
            app._persist_commercial_review_result(commercial_result, 1)
            with open(
                os.path.join(root, "narrative_audits", "chapter_0001.json"),
                "r",
                encoding="utf-8",
            ) as handle:
                persisted_audit = json.load(handle)
        self.assertEqual("deepseek-v4-pro", persisted_audit["model"])
        self.assertEqual(
            "historical-commercial-model",
            persist_commercial.call_args.kwargs["model_name"],
        )

    def test_export_blocks_unaudited_or_review_chapters_without_override(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "publish": os.path.join(root, "publish"),
                "logs": os.path.join(root, "logs"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            chapter_text = "第1章 开门\n\n林舟推开旧站铁门。"
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(chapter_text)
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {
                "narrative_guard_enabled": True,
                "narrative_guard_min_score": 75,
            }
            app._append_batch_audit = lambda *args, **kwargs: None
            app._commercial_publish_release_ready = lambda: False
            audit = self._valid_narrative_audit(chapter_text)
            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch("gui_app.messagebox.showerror") as showerror, \
                 mock.patch("gui_app.filedialog.asksaveasfilename") as save_dialog:
                app._record_chapter_status(
                    1, "正式可用", [], chapter_path, 10
                )
                app.export_book()
                self.assertTrue(showerror.called)
                save_dialog.assert_not_called()

                narrative_guard.persist_audit(
                    dirs["plot"],
                    1,
                    chapter_text,
                    audit,
                    model_name="deepseek-v4-pro",
                )
                os.remove(os.path.join(dirs["logs"], "chapter_status.jsonl"))
                app._record_chapter_status(
                    1,
                    "可继续但需复核",
                    ["质检预警：人物动机不足"],
                    chapter_path,
                    10,
                    narrative_audit_result=audit,
                )
                showerror.reset_mock()
                app.export_book()
                self.assertTrue(showerror.called)
                self.assertIn("待复核章节", showerror.call_args.args[1])
                save_dialog.assert_not_called()

    def test_incomplete_book_exports_only_as_preview(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = {
                "out": os.path.join(root, "output"),
                "publish": os.path.join(root, "publish"),
                "logs": os.path.join(root, "logs"),
                "plot": os.path.join(root, "plot"),
            }
            for path in dirs.values():
                os.makedirs(path)
            chapter_path = os.path.join(dirs["out"], "\u7b2c001\u7ae0.txt")
            chapter_text = "\u7b2c1\u7ae0 \u5f00\u95e8\n\n\u6797\u821f\u63a8\u5f00\u65e7\u7ad9\u94c1\u95e8\u3002"
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(chapter_text)

            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {"narrative_guard_enabled": False}
            app._commercial_publish_release_ready = lambda: False
            app._get_story_planned_end_chapter = lambda: 30
            app._load_latest_chapter_statuses = lambda: {
                1: {
                    "status": "\u6b63\u5f0f\u53ef\u7528",
                    "chapter_sha256": state_ledger.chapter_sha256(chapter_text),
                }
            }
            preview_path = os.path.join(root, "preview.txt")

            with mock.patch.dict(generator.DIRS, dirs, clear=True), \
                 mock.patch("gui_app.generator.audit_chapter_layout", return_value={
                     "missing": [], "duplicates": {}, "misplaced": []
                 }), \
                 mock.patch("gui_app.messagebox.showerror") as showerror, \
                 mock.patch("gui_app.messagebox.showinfo"), \
                 mock.patch("gui_app.filedialog.asksaveasfilename", return_value=preview_path) as save_dialog:
                app.export_book()
                self.assertTrue(showerror.called)
                self.assertIn("\u89c4\u5212\u7ec8\u7ae0", showerror.call_args.args[1])
                save_dialog.assert_not_called()

                showerror.reset_mock()
                app.export_book(preview=True)
                showerror.assert_not_called()
                save_dialog.assert_called_once()
                self.assertTrue(os.path.exists(preview_path))
                with open(preview_path, "r", encoding="utf-8") as handle:
                    self.assertIn("\u6797\u821f\u63a8\u5f00\u65e7\u7ad9\u94c1\u95e8", handle.read())

    def test_review_status_can_never_publish_via_legacy_override(self):
        app = object.__new__(gui_app.NovelGeneratorGUI)
        app.config = {"publish_review_chapters": True}
        app._commercial_publish_release_required = lambda: False
        app._remove_publish_copy = lambda chapter: False
        app._copy_to_publish = mock.Mock(
            side_effect=AssertionError("待复核章不得复制到发布目录")
        )
        self.assertEqual(
            "",
            app._publish_committed_chapter(
                "unused.txt", 1, "可继续但需复核"
            ),
        )
        app._copy_to_publish.assert_not_called()

    def test_review_rewrite_withdraws_stale_publish_copy(self):
        with tempfile.TemporaryDirectory() as root:
            publish_dir = os.path.join(root, "publish")
            os.makedirs(publish_dir)
            stale = os.path.join(publish_dir, "第0001章.txt")
            with open(stale, "w", encoding="utf-8") as handle:
                handle.write("旧的可发布版本")
            app = object.__new__(gui_app.NovelGeneratorGUI)
            app.config = {}
            app._commercial_publish_release_required = lambda: False
            with mock.patch.dict(
                generator.DIRS, {"publish": publish_dir}, clear=True
            ):
                message = app._publish_committed_chapter(
                    "unused.txt", 1, "可继续但需复核"
                )
            self.assertFalse(os.path.exists(stale))
            self.assertIn("撤回", message)


if __name__ == "__main__":
    unittest.main()
