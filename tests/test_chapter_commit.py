# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from unittest import mock

import chapter_commit
import chapter_validator
import generator
import narrative_guard
from gui_app import NovelGeneratorGUI


CHAR_LIMITS = {"min": 10, "max": 120}
REVIEW_MODEL = "deepseek-v4-pro"


def write_semantic_invariants(plot_dir):
    os.makedirs(plot_dir, exist_ok=True)
    path = os.path.join(plot_dir, "semantic_invariants.json")
    with open(path, "w", encoding="utf-8") as handle:
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
    return path


def valid_text(chapter=1, extra=""):
    return (
        f"第{chapter}章 旧站\n\n"
        f"林舟在旧站醒来，确认门锁完好，随后把现场编号写进记录簿。{extra}"
    )


def valid_validation_receipt(plot_dir, text, chapter=1, limits=None):
    limits = dict(limits or CHAR_LIMITS)
    report = chapter_validator.validate_candidate(plot_dir, chapter, text, limits)
    if not report.get("passed"):
        raise AssertionError(report)
    return chapter_validator.build_validation_receipt(report)


def valid_narrative_audit(chapter_text, chapter=1, model=REVIEW_MODEL):
    system_prompt = "你是自动叙事审查模型。"
    user_prompt = f"审查第{chapter}章。"
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
        "mainline_progress": "林舟在旧站开始行动",
        "progress_evidence_quote": "林舟在旧站醒来",
        "drift_flags": [],
        "nonsense_flags": [],
        "fail_reasons": [],
        "summary": "自动审查通过",
    }
    raw_response = json.dumps(payload, ensure_ascii=False)
    normalized, issues = narrative_guard.validate_audit(
        payload,
        chapter_text=chapter_text,
        requirements=[],
    )
    if issues:
        raise AssertionError(issues)
    normalized.update({
        "chapter": chapter,
        "chapter_sha256": chapter_commit.chapter_sha256(chapter_text),
        "review_model": model,
        "review_raw_response": raw_response,
    })
    normalized["review_receipt"] = narrative_guard.build_review_receipt(
        chapter_text=chapter_text,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_response=raw_response,
        review_model=model,
        decision_payload=normalized,
    )
    return normalized


def begin_valid(plot_dir, chapter_path, text, *, chapter=1, **overrides):
    limits = dict(overrides.pop("char_limits", CHAR_LIMITS))
    kwargs = {
        "chapter": chapter,
        "chapter_path": chapter_path,
        "chapter_text": text,
        "chapter_outline": f"第{chapter}章 旧站",
        "narrative_audit_result": valid_narrative_audit(text, chapter),
        "validation_receipt": valid_validation_receipt(
            plot_dir, text, chapter, limits
        ),
        "char_limits": limits,
        "chapter_status": "正式可用",
    }
    kwargs.update(overrides)
    return chapter_commit.begin(plot_dir, **kwargs)


class ChapterCommitTests(unittest.TestCase):
    def _dirs(self, root):
        dirs = {
            "plot": os.path.join(root, "plot"),
            "out": os.path.join(root, "output"),
            "hist": os.path.join(root, "history"),
            "logs": os.path.join(root, "logs"),
            "publish": os.path.join(root, "publish"),
            "chars": os.path.join(root, "characters"),
            "world": os.path.join(root, "world_building"),
        }
        for path in dirs.values():
            os.makedirs(path, exist_ok=True)
        write_semantic_invariants(dirs["plot"])
        return dirs

    def test_prepared_only_journal_can_be_discarded(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            begin_valid(
                dirs["plot"], os.path.join(dirs["out"], "第0001章.txt"), text
            )
            self.assertTrue(chapter_commit.discard_unwritten(dirs["plot"]))
            self.assertEqual(chapter_commit.load(dirs["plot"]), {})

    def test_hash_mismatch_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            begin_valid(dirs["plot"], chapter_path, text)
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(valid_text(extra="正文后来被篡改。"))
            chapter_commit.mark_step(dirs["plot"], "chapter_saved")
            with self.assertRaisesRegex(chapter_commit.ChapterCommitError, "哈希不一致"):
                chapter_commit.verify_saved_chapter(chapter_commit.load(dirs["plot"]))

    def test_replacement_journal_can_be_discarded_while_original_is_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            original = valid_text(extra="这是原稿。")
            replacement = valid_text(extra="这是尚未写入的新稿。")
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(original)
            begin_valid(
                dirs["plot"], chapter_path, replacement,
                replacement=True,
                previous_chapter_sha256=chapter_commit.chapter_sha256(original),
            )
            self.assertTrue(chapter_commit.discard_unwritten(dirs["plot"]))
            with open(chapter_path, "r", encoding="utf-8") as handle:
                self.assertEqual(original, handle.read())

    def test_missing_validation_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            with self.assertRaisesRegex(chapter_commit.ChapterCommitError, "验收收据无效"):
                chapter_commit.begin(
                    dirs["plot"], chapter=1,
                    chapter_path=os.path.join(dirs["out"], "第0001章.txt"),
                    chapter_text=text,
                    narrative_audit_result=valid_narrative_audit(text),
                    char_limits=CHAR_LIMITS,
                )

    def test_simple_release_accepts_deterministic_receipt_without_model_audit(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            pending = chapter_commit.begin(
                dirs["plot"],
                chapter=1,
                chapter_path=chapter_path,
                chapter_text=text,
                narrative_audit_required=False,
                validation_receipt=valid_validation_receipt(
                    dirs["plot"], text
                ),
                char_limits=CHAR_LIMITS,
            )
            self.assertFalse(pending["narrative_audit_required"])
            self.assertIsNone(pending["narrative_audit_result"])
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            chapter_commit.mark_step(dirs["plot"], "chapter_saved")
            self.assertEqual(
                text,
                chapter_commit.verify_saved_chapter(
                    chapter_commit.load(dirs["plot"])
                ),
            )

    def test_strict_release_still_rejects_missing_model_audit(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            with self.assertRaisesRegex(
                chapter_commit.ChapterCommitError, "缺少逐章主线"
            ):
                chapter_commit.begin(
                    dirs["plot"],
                    chapter=1,
                    chapter_path=os.path.join(
                        dirs["out"], "第0001章.txt"
                    ),
                    chapter_text=text,
                    validation_receipt=valid_validation_receipt(
                        dirs["plot"], text
                    ),
                    char_limits=CHAR_LIMITS,
                )

    def test_old_validation_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            receipt = valid_validation_receipt(dirs["plot"], text)
            receipt["validation_version"] = 0
            with self.assertRaisesRegex(chapter_commit.ChapterCommitError, "验收收据无效"):
                chapter_commit.begin(
                    dirs["plot"], chapter=1,
                    chapter_path=os.path.join(dirs["out"], "第0001章.txt"),
                    chapter_text=text,
                    narrative_audit_result=valid_narrative_audit(text),
                    validation_receipt=receipt,
                    char_limits=CHAR_LIMITS,
                )

    def test_overlong_double_title_and_meta_candidates_are_rejected(self):
        candidates = {
            "超长": (
                valid_text(extra="现场记录持续增加。" * 20),
                "chapter_too_long",
            ),
            "双标题": (
                valid_text() + "\n\n第2章 伪标题\n\n林舟没有进入下一章。",
                "chapter_title_count_invalid",
            ),
            "元话语": (
                valid_text(extra="这是本章第一次正向兑现书名能力。"),
                "meta_narrative_leak",
            ),
        }
        for label, (text, expected_code) in candidates.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                dirs = self._dirs(root)
                report = chapter_validator.validate_candidate(
                    dirs["plot"], 1, text, CHAR_LIMITS
                )
                self.assertFalse(report["passed"])
                self.assertIn(
                    expected_code,
                    {item.get("code") for item in report.get("issues") or []},
                )
                # Deliberately use a receipt for a previously valid body; the
                # commit boundary must revalidate the actual candidate.
                clean = valid_text()
                receipt = valid_validation_receipt(dirs["plot"], clean)
                with self.assertRaisesRegex(chapter_commit.ChapterCommitError, "验收收据无效"):
                    chapter_commit.begin(
                        dirs["plot"], chapter=1,
                        chapter_path=os.path.join(dirs["out"], "第0001章.txt"),
                        chapter_text=text,
                        narrative_audit_result=valid_narrative_audit(text),
                        validation_receipt=receipt,
                        char_limits=CHAR_LIMITS,
                    )

    def test_manual_review_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            audit = valid_narrative_audit(text)
            audit["review_model"] = "manual-review"
            audit["review_receipt"]["review_model"] = "manual-review"
            with self.assertRaisesRegex(chapter_commit.ChapterCommitError, "审计调用收据无效"):
                chapter_commit.begin(
                    dirs["plot"], chapter=1,
                    chapter_path=os.path.join(dirs["out"], "第0001章.txt"),
                    chapter_text=text,
                    narrative_audit_result=audit,
                    validation_receipt=valid_validation_receipt(dirs["plot"], text),
                    char_limits=CHAR_LIMITS,
                )

    def test_non_official_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            text = valid_text()
            with self.assertRaisesRegex(chapter_commit.ChapterCommitError, "只有正式可用稿"):
                begin_valid(
                    dirs["plot"], os.path.join(dirs["out"], "第0001章.txt"),
                    text, chapter_status="可继续但需复核",
                )

    def test_recovery_rejects_text_or_semantic_config_change(self):
        for changed in ("text", "config"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as root:
                dirs = self._dirs(root)
                text = valid_text()
                chapter_path = os.path.join(dirs["out"], "第0001章.txt")
                begin_valid(dirs["plot"], chapter_path, text)
                with open(chapter_path, "w", encoding="utf-8") as handle:
                    handle.write(text if changed == "config" else valid_text(extra="篡改。"))
                chapter_commit.mark_step(dirs["plot"], "chapter_saved")
                if changed == "config":
                    config_path = os.path.join(dirs["plot"], "semantic_invariants.json")
                    with open(config_path, "r", encoding="utf-8") as handle:
                        config = json.load(handle)
                    config["forbidden_patterns"] = [
                        {"id": "new_rule", "patterns": ["永不出现"]}
                    ]
                    with open(config_path, "w", encoding="utf-8") as handle:
                        json.dump(config, handle, ensure_ascii=False)
                with self.assertRaises(chapter_commit.ChapterCommitError):
                    chapter_commit.verify_saved_chapter(chapter_commit.load(dirs["plot"]))

    def test_gui_replays_post_save_steps_without_model_call(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            chapter = valid_text()
            chapter_path = os.path.join(dirs["out"], "第0001章.txt")
            begin_valid(dirs["plot"], chapter_path, chapter)
            with open(chapter_path, "w", encoding="utf-8") as handle:
                handle.write(chapter)
            chapter_commit.mark_step(dirs["plot"], "chapter_saved")

            app = NovelGeneratorGUI.__new__(NovelGeneratorGUI)
            app.config = {
                "canon_guard_enabled": False,
                "temporal_memory_enabled": True,
                "knowledge_writeback_interval": 10,
                "publish_review_chapters": False,
                "narrative_guard_min_score": 75,
            }
            app.project_dir = root
            app._append_batch_audit = lambda *args, **kwargs: None
            app._commercial_publish_release_required = lambda: False
            app.get_review_model_name = lambda: REVIEW_MODEL
            app.call_llm_review = lambda *args, **kwargs: self.fail("恢复不应调用模型")

            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                recovered = app._recover_pending_chapter_commit()
                again = app._recover_pending_chapter_commit()

            self.assertEqual(recovered["status"], "RECOVERED")
            self.assertEqual(again["status"], "NONE")
            self.assertTrue(os.path.exists(os.path.join(
                dirs["plot"], "narrative_audits", "chapter_0001.json"
            )))
            self.assertTrue(os.path.exists(os.path.join(dirs["publish"], "第0001章.txt")))
            receipts = os.listdir(os.path.join(dirs["plot"], "runtime", "commit_receipts"))
            self.assertEqual(len(receipts), 1)


if __name__ == "__main__":
    unittest.main()
