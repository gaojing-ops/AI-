import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import formal_suffix_rewrite
import gui_app


class FormalSuffixRewriteTests(unittest.TestCase):
    def test_cache_rotation_preserves_pending_failures_and_candidate_markers(self):
        with tempfile.TemporaryDirectory() as root:
            protected = []
            for i, (stage, payload) in enumerate([
                ('structured_state_rejected_pending', {'status': 'PENDING', 'payload_sha256': 'b' * 64}),
                ('manual_structured_state_rejected', {'status': 'REJECTED', 'repair_history': [{'attempt': 6}]}),
                ('state_only_candidate', {'scope': 'state_only'}),
            ]):
                fingerprint = hashlib.sha256(f'protected-{i}'.encode()).hexdigest()
                path = formal_suffix_rewrite.store_preflight_stage(root, chapter=1,
                    context_fingerprint=fingerprint, stage=stage, payload=payload)
                os.utime(path, (1, 1))
                protected.append((fingerprint, stage, payload, path))
            for i in range(12):
                formal_suffix_rewrite.store_preflight_stage(root, chapter=1,
                    context_fingerprint=hashlib.sha256(f'approval-{i}'.encode()).hexdigest(),
                    stage='narrative_audit', payload={'pass': True})
            for fingerprint, stage, payload, path in protected:
                self.assertTrue(Path(path).is_file())
                self.assertEqual(payload, formal_suffix_rewrite.load_preflight_stage(root, chapter=1,
                    context_fingerprint=fingerprint, stage=stage, strict=True))
            files = list(Path(protected[0][3]).parent.glob('*.json'))
            self.assertLessEqual(len(files), 3 + formal_suffix_rewrite.PREFLIGHT_CACHE_MAX_PER_CHAPTER)

    def test_cache_rotation_keeps_corrupt_record_for_diagnosis(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(formal_suffix_rewrite.store_preflight_stage(root, chapter=1,
                context_fingerprint='a' * 64, stage='structured_state_rejected', payload={'status': 'REJECTED'}))
            path.write_text('{broken', encoding='utf-8')
            os.utime(path, (1, 1))
            for i in range(9):
                formal_suffix_rewrite.store_preflight_stage(root, chapter=1,
                    context_fingerprint=hashlib.sha256(str(i).encode()).hexdigest(), stage='approved', payload={})
            self.assertEqual('{broken', path.read_text(encoding='utf-8'))

    def test_commercial_json_fingerprint_ignores_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first.json"
            second = Path(temp) / "second.json"
            first.write_text(
                json.dumps({
                    "updated_at": "2026-08-25T11:45:54",
                    "review_history": [{
                        "report_path": "D:/nsp_first/p/review_reports/review.md",
                        "decision": "PASS",
                    }],
                }),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps({
                    "updated_at": "2026-08-25T11:46:05",
                    "review_history": [{
                        "report_path": "D:/nsp_second/p/review_reports/review.md",
                        "decision": "PASS",
                    }],
                }),
                encoding="utf-8",
            )

            self.assertEqual(
                gui_app.NovelGeneratorGUI._formal_suffix_source_digest(
                    first, normalize_volatile=True
                ),
                gui_app.NovelGeneratorGUI._formal_suffix_source_digest(
                    second, normalize_volatile=True
                ),
            )

    def test_cached_release_guard_is_bound_to_exact_chapter_text(self):
        content = "第41章\n\n通过设定总校的候选正文。"
        payload = {
            "chapter": 41,
            "chapter_sha256": gui_app.state_ledger.chapter_sha256(content),
            "status": "PASS",
            "summary": "通过",
        }

        accepted = (
            gui_app.NovelGeneratorGUI._validate_cached_release_guard_preflight(
                payload, 41, content
            )
        )
        self.assertEqual("PASS", accepted["status"])
        with self.assertRaises(RuntimeError):
            gui_app.NovelGeneratorGUI._validate_cached_release_guard_preflight(
                payload, 41, content + "改动"
            )

    def test_micro_edit_accepts_bounded_wording_change_and_tracks_unchanged_tail(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        chapter2 = project / "drafts/第01卷/第0002章.txt"
        chapter3 = project / "drafts/第01卷/第0003章.txt"
        chapter2.write_text("第2章 原稿\n\n原始内容。", encoding="utf-8")
        chapter3.write_text("第3章 原稿\n\n原始内容3。", encoding="utf-8")

        report = formal_suffix_rewrite.validate_micro_edit_candidates(
            project,
            {2: chapter2, 3: chapter3},
        )

        self.assertEqual([2], report["changed_chapters"])
        self.assertEqual([3], report["unchanged_chapters"])
        self.assertLess(report["chapters"]["2"]["changed_ratio"], 0.08)

    def test_sparse_micro_edit_fills_unchanged_tail_from_formal_output(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidate = project / "drafts/第01卷/第0002章.txt"
        candidate.write_text("第2章 原稿\n\n原始内容2，稍作润色。", encoding="utf-8")

        resolved = formal_suffix_rewrite.resolve_sparse_micro_edit_candidates(
            project,
            {2: candidate},
            start_chapter=2,
            end_chapter=3,
        )

        self.assertEqual([2, 3], sorted(resolved))
        self.assertEqual(
            project / "output/第01卷/第0003章.txt",
            resolved[3],
        )

    def test_transaction_preloads_reused_formal_candidate_before_tail_removal(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidate = project / "drafts/第01卷/第0002章.txt"
        candidate.write_text("第2章 原稿\n\n原始内容2，稍作润色。", encoding="utf-8")
        resolved = formal_suffix_rewrite.resolve_sparse_micro_edit_candidates(
            project,
            {2: candidate},
            start_chapter=2,
            end_chapter=3,
        )

        result = formal_suffix_rewrite.execute_formal_suffix_rewrite(
            project,
            start_chapter=2,
            end_chapter=3,
            candidates=resolved,
            prepare_suffix=self.simple_prepare(project),
            commit_chapter=self.simple_commit(project),
        )

        self.assertEqual("COMPLETED", result["status"])
        self.assertIn(
            "原始内容3",
            (project / "output/第01卷/第0003章.txt").read_text(encoding="utf-8"),
        )

    def test_micro_edit_rejects_title_large_rewrite_and_new_date_fact(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidate = project / "drafts/第01卷/第0002章.txt"
        cases = (
            ("第2章 新标题\n\n原始内容2。", "不能修改章标题", {}),
            ("第2章 原稿\n\n" + "完全重写" * 20, "超过微调上限", {}),
            (
                "第2章 原稿\n\n原始内容2。六月一日复训。",
                "新增时间或数量事实",
                {"max_changed_ratio": 1.0},
            ),
        )
        for text, message, options in cases:
            with self.subTest(message=message):
                candidate.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(
                    formal_suffix_rewrite.FormalSuffixRewriteError, message
                ):
                    formal_suffix_rewrite.validate_micro_edit_candidates(
                        project, {2: candidate}, **options
                    )

    def make_project(self):
        temp = tempfile.TemporaryDirectory()
        project = Path(temp.name) / "project"
        for relative in (
            "output/第01卷",
            "publish",
            "plot/runtime",
            "history",
            "logs",
            "characters",
            "world_building",
            "drafts/第01卷",
        ):
            (project / relative).mkdir(parents=True, exist_ok=True)
        for chapter in range(1, 4):
            text = f"第{chapter}章 原稿\n\n原始内容{chapter}。"
            (project / "output" / "第01卷" / f"第{chapter:04d}章.txt").write_text(
                text, encoding="utf-8"
            )
            (project / "publish" / f"第{chapter:04d}章.txt").write_text(
                text, encoding="utf-8"
            )
        (project / "plot" / "state.json").write_text("old-state", encoding="utf-8")
        return temp, project

    def make_candidates(self, project, start=2, end=3):
        candidates = {}
        for chapter in range(start, end + 1):
            path = project / "drafts" / "第01卷" / f"第{chapter:04d}章.txt"
            path.write_text(f"第{chapter}章 新稿\n\n候选内容{chapter}。\n", encoding="utf-8")
            candidates[chapter] = path
        return candidates

    def simple_prepare(self, project):
        def prepare(start, end):
            for chapter in range(start, end + 1):
                (project / "output" / "第01卷" / f"第{chapter:04d}章.txt").unlink()
                (project / "publish" / f"第{chapter:04d}章.txt").unlink()
            (project / "plot" / "state.json").write_text("base-state", encoding="utf-8")

        return prepare

    def simple_commit(self, project, *, fail_at=0, mutate_prefix=False):
        def commit(chapter, target, text):
            if mutate_prefix:
                prefix = project / "output" / "第01卷" / "第0001章.txt"
                prefix.write_text("tampered", encoding="utf-8")
            if fail_at and chapter == fail_at:
                raise RuntimeError("forced failure")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            (project / "publish" / target.name).write_text(text, encoding="utf-8")
            (project / "plot" / "state.json").write_text(
                f"state-{chapter}", encoding="utf-8"
            )

        return commit

    def test_success_commits_complete_tail_and_keeps_next_draft_isolated(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        chapter4 = project / "drafts" / "第01卷" / "第0004章.txt"
        chapter4.write_text("第4章 仅草稿", encoding="utf-8")

        result = formal_suffix_rewrite.execute_formal_suffix_rewrite(
            project,
            start_chapter=2,
            end_chapter=3,
            candidates=candidates,
            prepare_suffix=self.simple_prepare(project),
            commit_chapter=self.simple_commit(project),
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertIn("候选内容2", (project / "output/第01卷/第0002章.txt").read_text(encoding="utf-8"))
        self.assertEqual((project / "plot/state.json").read_text(encoding="utf-8"), "state-3")
        self.assertFalse((project / "output/第01卷/第0004章.txt").exists())
        self.assertTrue(chapter4.exists())
        self.assertFalse((project / ".runtime" / formal_suffix_rewrite.MARKER_NAME).exists())

    def test_rejects_non_tail_range_before_writing(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project, 1, 2)

        with self.assertRaisesRegex(
            formal_suffix_rewrite.FormalSuffixRewriteError, "当前最新章为3"
        ):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=1,
                end_chapter=2,
                candidates=candidates,
                prepare_suffix=lambda _start, _end: None,
                commit_chapter=lambda _chapter, _target, _text: None,
            )

    def test_failure_restores_every_tracked_byte(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        before = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for root in formal_suffix_rewrite.DEFAULT_TRACKED_PATHS
            for path in (project / root).rglob("*")
            if path.is_file()
        }

        with self.assertRaisesRegex(
            formal_suffix_rewrite.FormalSuffixRewriteError, "已恢复事务前状态"
        ):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=2,
                end_chapter=3,
                candidates=candidates,
                prepare_suffix=self.simple_prepare(project),
                commit_chapter=self.simple_commit(project, fail_at=3),
            )

        after = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for root in formal_suffix_rewrite.DEFAULT_TRACKED_PATHS
            for path in (project / root).rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertFalse((project / ".runtime" / formal_suffix_rewrite.MARKER_NAME).exists())
        manifests = list(
            (project / ".runtime" / formal_suffix_rewrite.ARCHIVE_DIRNAME).glob("*/manifest.json")
        )
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text(encoding="utf-8"))["status"], "rolled_back")

    def test_prefix_hash_guard_triggers_full_rollback(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        prefix = project / "output/第01卷/第0001章.txt"
        original = prefix.read_bytes()

        with self.assertRaisesRegex(
            formal_suffix_rewrite.FormalSuffixRewriteError, "封存前缀第1章"
        ):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=2,
                end_chapter=3,
                candidates=candidates,
                prepare_suffix=self.simple_prepare(project),
                commit_chapter=self.simple_commit(project, mutate_prefix=True),
            )
        self.assertEqual(prefix.read_bytes(), original)

    def test_pending_single_chapter_commit_blocks_transaction(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        (project / "plot/runtime/pending_chapter_commit.json").write_text(
            "{}", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            formal_suffix_rewrite.FormalSuffixRewriteError, "未完成的单章正式提交"
        ):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=2,
                end_chapter=3,
                candidates=candidates,
                prepare_suffix=lambda _start, _end: None,
                commit_chapter=lambda _chapter, _target, _text: None,
            )

    def test_preflight_cache_round_trip_and_context_invalidation(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        fingerprint = formal_suffix_rewrite.build_preflight_fingerprint({
            "chapter_sha256": "a" * 64,
            "predecessor": {"chapter": 1, "hash": "b" * 64},
        })
        payload = {
            "chapter": 2,
            "chapter_sha256": "a" * 64,
            "delta": {"events": []},
        }

        cache_path = formal_suffix_rewrite.store_preflight_stage(
            project,
            chapter=2,
            context_fingerprint=fingerprint,
            stage="structured_state",
            payload=payload,
        )

        self.assertTrue(Path(cache_path).is_file())
        self.assertEqual(
            payload,
            formal_suffix_rewrite.load_preflight_stage(
                project,
                chapter=2,
                context_fingerprint=fingerprint,
                stage="structured_state",
            ),
        )
        changed = formal_suffix_rewrite.build_preflight_fingerprint({
            "chapter_sha256": "c" * 64,
            "predecessor": {"chapter": 1, "hash": "b" * 64},
        })
        self.assertIsNone(
            formal_suffix_rewrite.load_preflight_stage(
                project,
                chapter=2,
                context_fingerprint=changed,
                stage="structured_state",
            )
        )

    def test_rejected_state_hint_is_never_loaded_as_passed_state(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        fingerprint = formal_suffix_rewrite.build_preflight_fingerprint({'chapter': 74})
        hint = {'schema_version': 1, 'status': 'REJECTED', 'chapter': 74,
                'chapter_sha256': 'a' * 64, 'previous_raw': '{}', 'issues': ['漏记背心归还']}
        formal_suffix_rewrite.store_preflight_stage(
            project, chapter=74, context_fingerprint=fingerprint,
            stage='structured_state_rejected', payload=hint)
        self.assertEqual(hint, formal_suffix_rewrite.load_preflight_stage(
            project, chapter=74, context_fingerprint=fingerprint,
            stage='structured_state_rejected'))
        self.assertIsNone(formal_suffix_rewrite.load_preflight_stage(
            project, chapter=74, context_fingerprint=fingerprint,
            stage='structured_state'))

    def test_tampered_preflight_cache_is_rejected(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        fingerprint = formal_suffix_rewrite.build_preflight_fingerprint({"v": 1})
        cache_path = Path(formal_suffix_rewrite.store_preflight_stage(
            project,
            chapter=2,
            context_fingerprint=fingerprint,
            stage="narrative_audit",
            payload={"verdict": "PASS"},
        ))
        record = json.loads(cache_path.read_text(encoding="utf-8"))
        record["stages"]["narrative_audit"]["payload"]["verdict"] = "FAIL"
        cache_path.write_text(json.dumps(record), encoding="utf-8")

        self.assertIsNone(
            formal_suffix_rewrite.load_preflight_stage(
                project,
                chapter=2,
                context_fingerprint=fingerprint,
                stage="narrative_audit",
            )
        )

    def test_preflight_cache_survives_late_transaction_rollback(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        fingerprint = formal_suffix_rewrite.build_preflight_fingerprint({
            "chapter": 2,
            "candidate": "stable",
        })

        def commit(chapter, target, text):
            if chapter == 2:
                formal_suffix_rewrite.store_preflight_stage(
                    project,
                    chapter=2,
                    context_fingerprint=fingerprint,
                    stage="narrative_audit",
                    payload={"verdict": "PASS"},
                )
            self.simple_commit(project, fail_at=3)(chapter, target, text)

        with self.assertRaises(formal_suffix_rewrite.FormalSuffixRewriteError):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=2,
                end_chapter=3,
                candidates=candidates,
                prepare_suffix=self.simple_prepare(project),
                commit_chapter=commit,
            )

        self.assertEqual(
            {"verdict": "PASS"},
            formal_suffix_rewrite.load_preflight_stage(
                project,
                chapter=2,
                context_fingerprint=fingerprint,
                stage="narrative_audit",
            ),
        )

    def test_progress_callback_reports_chapter_boundaries(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        events = []

        formal_suffix_rewrite.execute_formal_suffix_rewrite(
            project,
            start_chapter=2,
            end_chapter=3,
            candidates=candidates,
            prepare_suffix=self.simple_prepare(project),
            commit_chapter=self.simple_commit(project),
            progress_callback=events.append,
        )

        names = [row["event"] for row in events]
        self.assertIn("snapshot_started", names)
        self.assertIn("prepare_completed", names)
        self.assertEqual(
            [2, 3],
            [row["chapter"] for row in events if row["event"] == "chapter_started"],
        )
        self.assertEqual("completed", names[-1])

    def test_live_rewrite_lock_rejects_overlap_before_snapshot(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        runtime = project / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / formal_suffix_rewrite.REWRITE_LOCK_NAME).write_text(
            json.dumps({
                "pid": os.getpid(),
                "task": "first rewrite",
                "token": "live",
            }),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            formal_suffix_rewrite.FormalSuffixRewriteError,
            "已有正式尾段任务正在运行",
        ):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=2,
                end_chapter=3,
                candidates=candidates,
                prepare_suffix=self.simple_prepare(project),
                commit_chapter=self.simple_commit(project),
            )
        self.assertFalse(
            (runtime / formal_suffix_rewrite.ARCHIVE_DIRNAME).exists()
        )

    def test_interrupted_transaction_is_explicitly_recovered(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        before = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for root in formal_suffix_rewrite.DEFAULT_TRACKED_PATHS
            for path in (project / root).rglob("*")
            if path.is_file()
        }

        def interrupt(chapter, target, text):
            self.simple_commit(project)(chapter, target, text)
            raise KeyboardInterrupt("forced interruption")

        with self.assertRaises(KeyboardInterrupt):
            formal_suffix_rewrite.execute_formal_suffix_rewrite(
                project,
                start_chapter=2,
                end_chapter=3,
                candidates=candidates,
                prepare_suffix=self.simple_prepare(project),
                commit_chapter=interrupt,
            )

        marker = project / ".runtime" / formal_suffix_rewrite.MARKER_NAME
        self.assertTrue(marker.exists())
        result = formal_suffix_rewrite.recover_pending_formal_suffix_rewrite(project)
        self.assertEqual("RECOVERED", result["status"])
        after = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for root in formal_suffix_rewrite.DEFAULT_TRACKED_PATHS
            for path in (project / root).rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertFalse(marker.exists())

    def test_isolated_preflight_clone_keeps_live_project_untouched_and_promotes_cache(self):
        temp, project = self.make_project()
        self.addCleanup(temp.cleanup)
        candidates = self.make_candidates(project)
        archive_sentinel = (
            project
            / ".runtime"
            / formal_suffix_rewrite.ARCHIVE_DIRNAME
            / "old"
            / "sentinel.txt"
        )
        archive_sentinel.parent.mkdir(parents=True, exist_ok=True)
        archive_sentinel.write_text("large archive", encoding="utf-8")
        destination = Path(temp.name) / "isolated"

        isolated, isolated_candidates = (
            formal_suffix_rewrite.create_isolated_preflight_project(
                project,
                destination,
                candidates,
            )
        )

        self.assertFalse(
            (isolated / ".runtime" / formal_suffix_rewrite.ARCHIVE_DIRNAME).exists()
        )
        self.assertEqual([2, 3], sorted(isolated_candidates))
        self.assertEqual(
            candidates[2].read_bytes(),
            isolated_candidates[2].read_bytes(),
        )
        fingerprint = formal_suffix_rewrite.build_preflight_fingerprint(
            {"isolated": True}
        )
        formal_suffix_rewrite.store_preflight_stage(
            isolated,
            chapter=2,
            context_fingerprint=fingerprint,
            stage="narrative_audit",
            payload={"verdict": "PASS"},
        )
        self.assertEqual(
            1,
            formal_suffix_rewrite.promote_isolated_preflight_cache(
                isolated,
                project,
            ),
        )
        self.assertEqual(
            {"verdict": "PASS"},
            formal_suffix_rewrite.load_preflight_stage(
                project,
                chapter=2,
                context_fingerprint=fingerprint,
                stage="narrative_audit",
            ),
        )
        self.assertEqual(
            0,
            formal_suffix_rewrite.promote_isolated_preflight_cache(
                isolated,
                project,
            ),
        )
        self.assertTrue(archive_sentinel.exists())


if __name__ == "__main__":
    unittest.main()
