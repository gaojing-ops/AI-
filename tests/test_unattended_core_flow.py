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
import state_ledger
from gui_app import NovelGeneratorGUI, _atomic_write_text


CHAR_LIMITS = {"min": 10, "max": 160}
REVIEW_MODEL = "deepseek-v4-pro"


def write_semantic_invariants(plot_dir):
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


def automatic_narrative_audit(text, chapter, evidence):
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
        "mainline_progress": f"林舟完成第{chapter}次记录",
        "progress_evidence_quote": evidence,
        "drift_flags": [],
        "nonsense_flags": [],
        "fail_reasons": [],
        "summary": "自动审查通过",
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
        "chapter_sha256": chapter_commit.chapter_sha256(text),
        "review_model": REVIEW_MODEL,
        "review_raw_response": raw_response,
    })
    normalized["review_receipt"] = narrative_guard.build_review_receipt(
        chapter_text=text,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        raw_response=raw_response,
        review_model=REVIEW_MODEL,
        decision_payload=normalized,
    )
    return normalized


class UnattendedCoreFlowTests(unittest.TestCase):
    def test_state_extraction_prompt_forbids_overloaded_character_evidence(self):
        system_prompt, user_prompt = state_ledger.build_extraction_prompts(
            2,
            "第2章 测试\n\n林晚屿拒绝读取用户备注。",
            "",
            "",
        )
        self.assertIn("全部非空字段", system_prompt)
        self.assertIn("复合变化要拆成多个最小对象", system_prompt)
        self.assertIn("不得因为角色在本章出现就自动填写地点", user_prompt)
        self.assertIn("不得合并", user_prompt)
        self.assertIn("某人（保管）", user_prompt)
        self.assertIn("不得添加", user_prompt)
        self.assertIn("两个时间点之间的差值", user_prompt)
        self.assertIn("历史生成时间", user_prompt)
        self.assertIn("不得填成当前事件的 time_anchor", user_prompt)

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

    def test_three_chapters_complete_with_fake_deepseek_and_durable_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = self._dirs(root)
            app = NovelGeneratorGUI.__new__(NovelGeneratorGUI)
            app.project_dir = root
            app.config = {
                "structured_state_enabled": True,
                "state_delta_max_attempts": 2,
                "state_delta_independent_audit": True,
                "pre_save_continuity_audit_enabled": True,
                "temporal_memory_enabled": True,
                "temporal_memory_max_hits": 8,
                "temporal_memory_max_chars": 1200,
                "subplot_stale_warn_chapters": 8,
                "canon_guard_enabled": False,
                "knowledge_writeback_interval": 10,
                "publish_review_chapters": False,
            }
            app._append_batch_audit = lambda *args, **kwargs: None
            app._commercial_publish_release_required = lambda: False
            app.get_review_model_name = lambda: REVIEW_MODEL
            fake = {"delta": None, "calls": []}

            def fake_review(system_prompt, user_prompt, **kwargs):
                if "独立的小说正史证据与连续性审计员" in system_prompt:
                    fake["calls"].append("audit")
                    return json.dumps({"pass": True, "reject": []}, ensure_ascii=False)
                fake["calls"].append("extract")
                return json.dumps(fake["delta"], ensure_ascii=False)

            app.call_llm_review = fake_review

            with mock.patch.dict(generator.DIRS, dirs, clear=True):
                for chapter in range(1, 4):
                    sentence = (
                        f"林舟在记录牌上刻下数字{chapter}，核对旧站门锁后，"
                        "又把现场状态抄进随身记录簿。"
                    )
                    text = f"第{chapter}章 记录\n\n{sentence}"
                    fake["delta"] = {
                        "chapter": chapter,
                        "characters": [],
                        "resources": [],
                        "relationships": [],
                        "hooks": [],
                        "subplots": [],
                        "events": [{
                            "event_type": "other",
                            "summary": f"林舟完成第{chapter}次记录",
                            "evidence_quote": sentence,
                        }],
                    }
                    prepared = app._prepare_structured_state_delta(
                        chapter, text, f"第{chapter}章 记录"
                    )
                    path = os.path.join(dirs["out"], f"第{chapter}章.txt")
                    narrative_audit = automatic_narrative_audit(
                        text, chapter, sentence
                    )
                    validation_report = chapter_validator.validate_candidate(
                        dirs["plot"], chapter, text, CHAR_LIMITS
                    )
                    self.assertTrue(validation_report["passed"], validation_report)
                    validation_receipt = chapter_validator.build_validation_receipt(
                        validation_report
                    )
                    chapter_commit.begin(
                        dirs["plot"],
                        chapter=chapter,
                        chapter_path=path,
                        chapter_text=text,
                        chapter_outline=f"第{chapter}章 记录",
                        prepared_state_delta=prepared,
                        narrative_audit_result=narrative_audit,
                        validation_receipt=validation_receipt,
                        char_limits=CHAR_LIMITS,
                        chapter_status="正式可用",
                    )
                    _atomic_write_text(path, text)
                    chapter_commit.mark_step(dirs["plot"], "chapter_saved")
                    recovered = app._recover_pending_chapter_commit()
                    self.assertEqual("RECOVERED", recovered["status"])

            state = state_ledger.load_state(dirs["plot"])
            self.assertEqual(3, state["current_chapter"])
            self.assertEqual(3, state["delta_count"])
            self.assertEqual(6, len(fake["calls"]))
            self.assertEqual(["extract", "audit"] * 3, fake["calls"])
            self.assertTrue(os.path.exists(os.path.join(dirs["plot"], "story_memory.db")))
            self.assertTrue(os.path.exists(os.path.join(dirs["plot"], "关键事实库.json")))
            self.assertEqual(3, len(os.listdir(dirs["publish"])))
            receipts_dir = os.path.join(dirs["plot"], "runtime", "commit_receipts")
            self.assertEqual(3, len(os.listdir(receipts_dir)))
            with open(
                os.path.join(dirs["logs"], "chapter_status.jsonl"),
                "r",
                encoding="utf-8",
            ) as handle:
                statuses = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(3, len(statuses))
            self.assertTrue(all(row.get("chapter_sha256") for row in statuses))


if __name__ == "__main__":
    unittest.main()
