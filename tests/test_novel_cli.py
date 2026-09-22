import json
import hashlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import novel_cli
import state_ledger


class NovelCliTests(unittest.TestCase):
    def test_doctor_checks_all_present_fact_progress_aliases_without_writes(self):
        cases = ((2, 1, "WARN"), (2, 3, "FAIL"),
                 (2, None, "WARN"), (2, 2, "PASS"))
        for latest, current, expected in cases:
            with self.subTest(current=current), tempfile.TemporaryDirectory() as temp:
                project = self._doctor_ready_project(Path(temp))
                fact_path = project / "plot" / "关键事实库.json"
                fact_path.write_text(json.dumps({"latest_chapter": latest,
                                                  "current_chapter": current}),
                                     encoding="utf-8")
                before = self._tree_hashes(project)
                report = novel_cli.build_doctor_report(project)
                row = next(x for x in report["derived_state"]["items"]
                           if x["name"] == "关键事实库")
                self.assertEqual(expected, row["status"])
                self.assertEqual(expected, report["overall_status"])
                self.assertEqual(before, self._tree_hashes(project))

    def test_rolling_hold_requires_completed_hash_bound_historical_evidence(self):
        import commercial_reviewer

        for scenario in ('valid', 'missing', 'bad_digest', 'stale_body', 'unfinished', 'released'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                project = self._doctor_ready_project(Path(root), chapters=(1, 2, 3, 4))
                for chapter in (3, 4):
                    (project / 'publish' / '第01卷' / f'第{chapter:04d}章.txt').unlink()
                (project / 'project_config.json').write_text(json.dumps({
                    'release_mode': 'strict', 'hold_publish_until_commercial_pass': True,
                    'commercial_review_enabled': True,
                }), encoding='utf-8')
                def review_for(chapter):
                    digest = novel_cli._chapter_text_sha256(project / 'output' / '第01卷' / f'第{chapter:04d}章.txt')
                    review = {'chapter': chapter, 'status': 'WARN', 'current': 'PASS',
                              'action': 'ADJUST', 'raw': 'OVERALL: WARN\nCURRENT: PASS\nACTION: ADJUST\nSUMMARY: test warning\nDEBT_STATUS: RESOLVED\nDEBT_EVIDENCE:\ntest evidence', 'debt_status': 'RESOLVED',
                              'debt_evidence': ['test evidence'], 'reviewed_chapters': [chapter],
                              'chapter_hashes': {str(chapter): digest}}
                    review['review_receipt'] = commercial_reviewer.build_review_receipt(
                        'system', 'user', review['raw'], 'test-model', [chapter], review['chapter_hashes'])
                    return review
                report_dir = project / 'review_reports'
                report_dir.mkdir()
                (report_dir / 'latest_commercial_review.json').write_text(
                    json.dumps(review_for(4)), encoding='utf-8')
                self._write_formal_commit_receipt(project, 3)
                path = next((project / 'plot/runtime/commit_receipts').glob('chapter_0003_*.json'))
                payload = json.loads(path.read_text(encoding='utf-8'))
                payload.update(commercial_review_required=True, commercial_review_model='test-model',
                               commercial_review_result=review_for(3))
                payload['steps']['commercial_review_persisted'] = True
                if scenario == 'bad_digest':
                    payload['commercial_review_result']['review_receipt']['receipt_sha256'] = '0' * 64
                elif scenario == 'stale_body':
                    payload['chapter_sha256'] = '0' * 64
                elif scenario == 'unfinished':
                    payload['steps']['commercial_review_persisted'] = False
                elif scenario == 'released':
                    payload['commercial_review_result'].update(status='PASS', action='CONTINUE')
                path.write_text(json.dumps(payload), encoding='utf-8')
                if scenario == 'missing':
                    path.unlink()
                before = self._tree_hashes(project)
                result = novel_cli.build_doctor_report(project)
                mirror = next(row for row in result['checks'] if row['name'] == '发布镜像')
                self.assertEqual('PASS' if scenario == 'valid' else 'FAIL', mirror['status'])
                if scenario == 'valid':
                    self.assertEqual([3], mirror['historical_held_chapters'])
                self.assertEqual(before, self._tree_hashes(project))

    def test_doctor_rejects_corrupt_revision_debt_without_writes(self):
        with tempfile.TemporaryDirectory() as root:
            project = self._doctor_ready_project(Path(root))
            (project / "plot" / "revision_debt.json").write_text(
                "{truncated", encoding="utf-8"
            )
            before = self._tree_hashes(project)
            report = novel_cli.build_doctor_report(project)
            self.assertEqual(report["overall_status"], "FAIL")
            rows = report["checks"] + report["derived_state"]["items"]
            self.assertTrue(any(
                row["name"] == "修订债务账本" and row["status"] == "FAIL"
                for row in rows
            ))
            self.assertEqual(self._tree_hashes(project), before)

    def _project(self, root: Path, chapters=(1, 2)) -> Path:
        project = root / "book"
        (project / "output" / "第01卷").mkdir(parents=True)
        (project / "logs").mkdir()
        (project / "project_config.json").write_text("{}", encoding="utf-8")
        for number in chapters:
            (project / "output" / "第01卷" / f"第{number:04d}章.txt").write_text(
                f"第{number}章 测试\n\n正文", encoding="utf-8"
            )
        return project

    def _doctor_ready_project(self, root: Path, chapters=(1, 2)) -> Path:
        project = self._project(root, chapters=chapters)
        plot = project / "plot"
        plot.mkdir()
        (project / "publish" / "第01卷").mkdir(parents=True)
        status_rows = []
        for number in chapters:
            output = project / "output" / "第01卷" / f"第{number:04d}章.txt"
            publish = project / "publish" / "第01卷" / output.name
            publish.write_bytes(output.read_bytes())
            status_rows.append({
                "chapter": number,
                "status": "正式可用",
                "chapter_sha256": novel_cli._chapter_text_sha256(output),
            })
            (plot / "state_deltas").mkdir(exist_ok=True)
            (plot / "state_deltas" / f"chapter_{number:04d}.json").write_text(
                json.dumps({"chapter": number}), encoding="utf-8"
            )
            (plot / "canon_snapshots").mkdir(exist_ok=True)
            (plot / "canon_snapshots" / f"chapter_{number:04d}.json").write_text(
                json.dumps({"current_chapter": number}), encoding="utf-8"
            )
        latest = max(chapters, default=0)
        (plot / "canon_state.json").write_text(
            json.dumps({"current_chapter": latest}), encoding="utf-8"
        )
        (plot / "关键事实库.json").write_text(
            json.dumps({"latest_chapter": latest}), encoding="utf-8"
        )
        (plot / "story_state.json").write_text(
            json.dumps({"current_chapter": latest}), encoding="utf-8"
        )
        (project / "logs" / "chapter_status.jsonl").write_text(
            "\n".join(json.dumps(row) for row in status_rows) + "\n",
            encoding="utf-8",
        )
        return project

    @staticmethod
    def _tree_hashes(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
        }

    @staticmethod
    def _write_formal_commit_receipt(project: Path, chapter: int) -> None:
        output = project / "output" / "第01卷" / f"第{chapter:04d}章.txt"
        chapter_hash = novel_cli._chapter_text_sha256(output)
        receipt_dir = project / "plot" / "runtime" / "commit_receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / f"chapter_{chapter:04d}_{chapter_hash[:12]}.json").write_text(
            json.dumps({
                "chapter": chapter,
                "chapter_sha256": chapter_hash,
                "chapter_status": "正式可用",
                "commercial_review_required": False,
                "narrative_audit_result": {
                    "verdict": "PASS",
                    "chapter_sha256": chapter_hash,
                },
                "validation_receipt": {
                    "passed": True,
                    "chapter_sha256": chapter_hash,
                },
                "steps": {
                    "chapter_saved": True,
                    "state_committed": True,
                    "status_recorded": True,
                    "narrative_audit_persisted": True,
                    "publish_resolved": True,
                },
                "completed_at": "2026-08-20T15:25:38",
            }),
            encoding="utf-8",
        )

    @staticmethod
    def _prepare_strict_evidence_tail(project: Path, chapter: int = 2) -> tuple[Path, str]:
        output = project / "output" / "第01卷" / f"第{chapter:04d}章.txt"
        publish = project / "publish" / "第01卷" / output.name
        body = "".join(chr(0x4E00 + index) for index in range(3005))
        output.write_text(f"第{chapter}章 测试\n\n{body}", encoding="utf-8")
        publish.write_bytes(output.read_bytes())
        chapter_hash = novel_cli._chapter_text_sha256(output)
        plot = project / "plot"
        with tempfile.TemporaryDirectory() as temp_dir:
            clean_plot = Path(temp_dir)
            for number in range(1, chapter + 1):
                text = (project / "output" / "第01卷" / f"第{number:04d}章.txt").read_text(encoding="utf-8")
                quote = text[:80]
                delta, issues = state_ledger.validate_delta({
                    "chapter": number, "events": [{"event_type": "world",
                        "summary": "测试事件", "evidence_quote": quote}],
                }, number, text, state_ledger.load_state(clean_plot))
                assert not issues, issues
                state_ledger.commit_validated_delta(clean_plot, delta, text)
            for source in clean_plot.rglob("*"):
                if source.is_file():
                    target = plot / source.relative_to(clean_plot)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
        rows = []
        for raw in (project / "logs" / "chapter_status.jsonl").read_text(
            encoding="utf-8"
        ).splitlines():
            row = json.loads(raw)
            if row.get("chapter") == chapter:
                row["chapter_sha256"] = chapter_hash
            rows.append(row)
        (project / "logs" / "chapter_status.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        (project / "project_config.json").write_text(
            json.dumps({
                "release_mode": "strict",
                "doctor_evidence_enforcement": True,
                "legacy_evidence_exempt_through_chapter": chapter - 1,
                "chapter_char_min": 3000,
                "chapter_char_target_max": 4300,
                "review_model": "test-review-model",
                "narrative_guard_min_score": 78,
            }),
            encoding="utf-8",
        )
        return output, chapter_hash

    def test_inspect_project_uses_formal_output_and_batch_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir), chapters=(1, 2, 3))
            (project / "logs" / "batch_state.json").write_text(
                json.dumps({"status": "paused"}), encoding="utf-8"
            )

            status = novel_cli.inspect_project(project)

            self.assertEqual(3, status["latest_formal_chapter"])
            self.assertEqual(4, status["next_chapter"])
            self.assertEqual([], status["missing_chapters"])
            self.assertEqual("paused", status["batch_state"]["status"])

    def test_inspect_project_reports_missing_chapter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir), chapters=(1, 3))
            status = novel_cli.inspect_project(project)
            self.assertEqual([2], status["missing_chapters"])

    def test_inspect_project_marks_paused_batch_superseded_by_formal_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir), chapters=(1, 2, 3))
            (project / "logs" / "batch_state.json").write_text(
                json.dumps({
                    "status": "paused",
                    "target_end": 3,
                    "skipped_chapters": [3],
                }),
                encoding="utf-8",
            )

            status = novel_cli.inspect_project(project)

            self.assertEqual("paused", status["batch_state"]["status"])
            self.assertEqual("superseded", status["batch_state"]["effective_status"])
            self.assertTrue(status["batch_state"]["superseded_by_formal_progress"])
            self.assertEqual(3, status["batch_state"]["superseded_at_chapter"])

    def test_inspect_project_keeps_active_paused_batch_effective(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir), chapters=(1, 2))
            (project / "logs" / "batch_state.json").write_text(
                json.dumps({"status": "paused", "target_end": 3}),
                encoding="utf-8",
            )

            status = novel_cli.inspect_project(project)

            self.assertEqual("paused", status["batch_state"]["effective_status"])
            self.assertFalse(status["batch_state"]["superseded_by_formal_progress"])

    def test_run_without_execute_fails_before_constructing_headless_app(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir), chapters=(1, 2))
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = novel_cli.main(
                    ["run", "--project", str(project), "--to", "3"]
                )
            self.assertEqual(2, code)
            self.assertIn("--execute", stderr.getvalue())

    def test_micro_edit_defaults_to_read_only_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self._project(root, chapters=(1, 2))
            candidate_dir = root / "candidates"
            candidate_dir.mkdir()
            (candidate_dir / "第0002章.txt").write_text(
                "第2章 测试\n\n正文。", encoding="utf-8"
            )
            before = self._tree_hashes(project)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main([
                    "micro-edit",
                    "--project", str(project),
                    "--start", "2",
                    "--end", "2",
                    "--candidates-dir", str(candidate_dir),
                    "--max-changed-ratio", "1",
                ])

            self.assertEqual(0, code)
            self.assertEqual(before, self._tree_hashes(project))
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["read_only"])
            self.assertEqual([2], payload["micro_edit"]["changed_chapters"])
            self.assertFalse((project / "output" / "第01卷" / "第0003章.txt").exists())

    def test_micro_edit_infers_sparse_range_and_reuses_unchanged_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = self._project(root, chapters=(1, 2, 3))
            candidate_dir = root / "candidates"
            candidate_dir.mkdir()
            (candidate_dir / "第0002章.txt").write_text(
                "第2章 测试\n\n正文。", encoding="utf-8"
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main([
                    "micro-edit",
                    "--project", str(project),
                    "--candidates-dir", str(candidate_dir),
                    "--max-changed-ratio", "1",
                ])

            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            self.assertEqual([2, 3], payload["execution_plan"]["range"])
            self.assertEqual([2], payload["execution_plan"]["explicit_candidates"])
            self.assertEqual([3], payload["execution_plan"]["reused_chapters"])

    def test_status_command_is_read_only_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._project(Path(temp_dir), chapters=(1,))
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["status", "--project", str(project)])
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(1, payload["latest_formal_chapter"])

    def test_doctor_reports_derived_state_without_writing_project_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2))
            before = self._tree_hashes(project)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, code)
            self.assertTrue(payload["read_only"])
            self.assertEqual("PASS", payload["overall_status"])
            self.assertEqual("PASS", payload["derived_state"]["overall_status"])
            self.assertEqual(before, self._tree_hashes(project))

    def test_doctor_fails_when_a_derived_state_leads_formal_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2))
            (project / "plot" / "story_state.json").write_text(
                json.dumps({"current_chapter": 3}), encoding="utf-8"
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])

            payload = json.loads(stdout.getvalue())
            state_item = next(
                item for item in payload["derived_state"]["items"]
                if item["name"] == "物化状态账本"
            )
            self.assertEqual(2, code)
            self.assertEqual("FAIL", state_item["status"])

    def test_doctor_evidence_enforcement_catches_hashless_formal_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2))
            (project / "project_config.json").write_text(
                json.dumps({
                    "release_mode": "strict",
                    "doctor_evidence_enforcement": True,
                    "legacy_evidence_exempt_through_chapter": 1,
                }),
                encoding="utf-8",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])

            payload = json.loads(stdout.getvalue())
            evidence = next(
                item for item in payload["derived_state"]["items"]
                if item["name"] == "正式证据链"
            )
            self.assertEqual(2, code)
            self.assertEqual("FAIL", evidence["status"])
            self.assertEqual([2], evidence["affected_chapters"])

    def test_doctor_evidence_enforcement_rejects_hash_bound_fake_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2))
            _output, chapter_hash = self._prepare_strict_evidence_tail(project)
            self._write_formal_commit_receipt(project, 2)

            report = novel_cli.build_doctor_report(project)

            evidence = next(
                item for item in report["derived_state"]["items"]
                if item["name"] == "正式证据链"
            )
            self.assertEqual("FAIL", evidence["status"])
            self.assertEqual([2], evidence["affected_chapters"])
            self.assertIn("提交收据版本无效", evidence["message"])
            self.assertIn("提交收据未要求叙事审计", evidence["message"])

    def test_doctor_evidence_enforcement_accepts_fully_verified_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2))
            _output, chapter_hash = self._prepare_strict_evidence_tail(project)
            receipt_dir = project / "plot" / "runtime" / "commit_receipts"
            receipt_dir.mkdir(parents=True)
            (receipt_dir / f"chapter_0002_{chapter_hash[:12]}.json").write_text(
                json.dumps({
                    "schema_version": 3,
                    "chapter": 2,
                    "chapter_sha256": chapter_hash,
                    "chapter_status": "正式可用",
                    "completed_at": "2026-08-28T12:00:00",
                    "narrative_audit_required": True,
                    "steps": {
                        "chapter_saved": True,
                        "state_committed": True,
                        "status_recorded": True,
                        "narrative_audit_persisted": True,
                        "publish_resolved": True,
                    },
                    "validation_receipt": {"receipt_type": "test"},
                    "narrative_audit_result": {
                        "verdict": "PASS",
                        "chapter_sha256": chapter_hash,
                    },
                }),
                encoding="utf-8",
            )
            persisted = {"verdict": "PASS", "chapter_sha256": chapter_hash}
            with (
                mock.patch(
                    "chapter_validator.verify_validation_receipt",
                    return_value={"receipt_valid": True},
                ) as validation_verify,
                mock.patch("narrative_guard.verify_review_receipt") as review_verify,
                mock.patch(
                    "narrative_guard.verify_persisted_audit",
                    return_value=persisted,
                ) as persisted_verify,
            ):
                report = novel_cli.build_doctor_report(project)

            evidence = next(
                item for item in report["derived_state"]["items"]
                if item["name"] == "正式证据链"
            )
            self.assertEqual("PASS", evidence["status"])
            validation_verify.assert_called_once()
            review_verify.assert_called_once()
            persisted_verify.assert_called_once()

    def test_doctor_rejects_semantically_stale_state_without_repairing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2))
            self._prepare_strict_evidence_tail(project)
            self._write_formal_commit_receipt(project, 2)
            plot = project / "plot"
            state = json.loads((plot / "story_state.json").read_text(encoding="utf-8"))
            state["resources"] = {"invented": {"quantity": 33}}
            state_ledger._write_materialized_state(plot, state)
            before = self._tree_hashes(project)
            report = novel_cli.build_doctor_report(project)
            evidence = next(item for item in report["derived_state"]["items"]
                            if item["name"] == "正式证据链")
            self.assertEqual("FAIL", evidence["status"])
            self.assertIn("重放不一致：resources", evidence["message"])
            self.assertEqual(before, self._tree_hashes(project))

    def test_doctor_accepts_hash_bound_commercial_publish_hold(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2, 3))
            (project / "publish" / "第01卷" / "第0003章.txt").unlink()
            (project / "project_config.json").write_text(
                json.dumps({
                    "release_mode": "strict",
                    "hold_publish_until_commercial_pass": True,
                    "commercial_review_enabled": True,
                }),
                encoding="utf-8",
            )
            chapter = project / "output" / "第01卷" / "第0003章.txt"
            chapter_hash = novel_cli._chapter_text_sha256(chapter)
            receipt = {
                "origin": "automatic_model_review",
                "reviewed_chapters": [3],
                "chapter_hashes": {"3": chapter_hash},
                "system_prompt_sha256": "1" * 64,
                "user_prompt_sha256": "2" * 64,
                "response_sha256": "3" * 64,
                "receipt_sha256": "4" * 64,
            }
            report_dir = project / "review_reports"
            report_dir.mkdir()
            (report_dir / "latest_commercial_review.json").write_text(
                json.dumps({
                    "chapter": 3,
                    "status": "WARN",
                    "current": "PASS",
                    "action": "ADJUST",
                    "reviewed_chapters": [3],
                    "chapter_hashes": {"3": chapter_hash},
                    "review_receipt": receipt,
                }),
                encoding="utf-8",
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])

            payload = json.loads(stdout.getvalue())
            mirror = next(item for item in payload["checks"] if item["name"] == "发布镜像")
            self.assertEqual(0, code)
            self.assertEqual("PASS", mirror["status"])
            self.assertEqual("commercial_hold", mirror["mode"])
            self.assertEqual([3], mirror["held_chapters"])

    def test_doctor_accepts_commercial_hold_inherited_by_nondue_formal_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2, 3, 4))
            for chapter in (3, 4):
                (project / "publish" / "第01卷" / f"第{chapter:04d}章.txt").unlink()
            (project / "project_config.json").write_text(
                json.dumps({
                    "release_mode": "strict",
                    "hold_publish_until_commercial_pass": True,
                    "commercial_review_enabled": True,
                    "commercial_review_interval": 10,
                }),
                encoding="utf-8",
            )
            chapter = project / "output" / "第01卷" / "第0003章.txt"
            chapter_hash = novel_cli._chapter_text_sha256(chapter)
            receipt = {
                "origin": "automatic_model_review",
                "reviewed_chapters": [3],
                "chapter_hashes": {"3": chapter_hash},
                "system_prompt_sha256": "1" * 64,
                "user_prompt_sha256": "2" * 64,
                "response_sha256": "3" * 64,
                "receipt_sha256": "4" * 64,
            }
            report_dir = project / "review_reports"
            report_dir.mkdir()
            (report_dir / "latest_commercial_review.json").write_text(
                json.dumps({
                    "chapter": 3,
                    "status": "WARN",
                    "current": "PASS",
                    "action": "ADJUST",
                    "reviewed_chapters": [3],
                    "chapter_hashes": {"3": chapter_hash},
                    "review_receipt": receipt,
                }),
                encoding="utf-8",
            )
            self._write_formal_commit_receipt(project, 4)

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])

            payload = json.loads(stdout.getvalue())
            mirror = next(item for item in payload["checks"] if item["name"] == "发布镜像")
            self.assertEqual(0, code)
            self.assertEqual("PASS", mirror["status"])
            self.assertEqual([3, 4], mirror["held_chapters"])
            self.assertEqual([4], mirror["inherited_chapters"])
            self.assertEqual(3, mirror["review_chapter"])

    def test_doctor_rejects_inherited_commercial_hold_without_tail_receipt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2, 3, 4))
            for chapter in (3, 4):
                (project / "publish" / "第01卷" / f"第{chapter:04d}章.txt").unlink()
            (project / "project_config.json").write_text(
                json.dumps({
                    "release_mode": "strict",
                    "hold_publish_until_commercial_pass": True,
                    "commercial_review_enabled": True,
                }),
                encoding="utf-8",
            )
            chapter = project / "output" / "第01卷" / "第0003章.txt"
            chapter_hash = novel_cli._chapter_text_sha256(chapter)
            report_dir = project / "review_reports"
            report_dir.mkdir()
            (report_dir / "latest_commercial_review.json").write_text(
                json.dumps({
                    "chapter": 3,
                    "status": "WARN",
                    "current": "PASS",
                    "action": "ADJUST",
                    "reviewed_chapters": [3],
                    "chapter_hashes": {"3": chapter_hash},
                    "review_receipt": {
                        "origin": "automatic_model_review",
                        "reviewed_chapters": [3],
                        "chapter_hashes": {"3": chapter_hash},
                        "system_prompt_sha256": "1" * 64,
                        "user_prompt_sha256": "2" * 64,
                        "response_sha256": "3" * 64,
                        "receipt_sha256": "4" * 64,
                    },
                }),
                encoding="utf-8",
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])

            payload = json.loads(stdout.getvalue())
            mirror = next(item for item in payload["checks"] if item["name"] == "发布镜像")
            self.assertEqual(2, code)
            self.assertEqual("FAIL", mirror["status"])

    def test_doctor_rejects_commercial_hold_with_wrong_chapter_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = self._doctor_ready_project(Path(temp_dir), chapters=(1, 2, 3))
            (project / "publish" / "第01卷" / "第0003章.txt").unlink()
            (project / "project_config.json").write_text(
                json.dumps({
                    "release_mode": "strict",
                    "hold_publish_until_commercial_pass": True,
                    "commercial_review_enabled": True,
                }),
                encoding="utf-8",
            )
            report_dir = project / "review_reports"
            report_dir.mkdir()
            (report_dir / "latest_commercial_review.json").write_text(
                json.dumps({
                    "chapter": 3,
                    "status": "WARN",
                    "current": "PASS",
                    "action": "ADJUST",
                    "reviewed_chapters": [3],
                    "chapter_hashes": {"3": "0" * 64},
                    "review_receipt": {
                        "origin": "automatic_model_review",
                        "reviewed_chapters": [3],
                        "chapter_hashes": {"3": "0" * 64},
                        "system_prompt_sha256": "1" * 64,
                        "user_prompt_sha256": "2" * 64,
                        "response_sha256": "3" * 64,
                        "receipt_sha256": "4" * 64,
                    },
                }),
                encoding="utf-8",
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = novel_cli.main(["doctor", "--project", str(project)])
            payload = json.loads(stdout.getvalue())
            mirror = next(item for item in payload["checks"] if item["name"] == "发布镜像")
            self.assertEqual(2, code)
            self.assertEqual("FAIL", mirror["status"])


if __name__ == "__main__":
    unittest.main()
