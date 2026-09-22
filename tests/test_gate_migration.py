# -*- coding: utf-8 -*-

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import gate_migration


def _text(chapter, revision=0):
    return f"第{chapter}章 测试\n\n这是第{chapter}章的冻结正文。修订轮次{revision}。\n"


def _failed(*chapters):
    return {
        "status": "FAIL",
        "current": "FAIL",
        "action": "PAUSE",
        "problem_chapters": list(chapters),
    }


def _review_result(request, *, passed, problems=()):
    return {
        "status": "PASS" if passed else "FAIL",
        "current": "PASS" if passed else "FAIL",
        "action": "CONTINUE" if passed else "PAUSE",
        "problem_chapters": list(problems),
        "reviewed_chapters": list(request.expected_chapters),
        "chapter_hashes": {
            str(number): digest for number, digest in request.chapter_hashes.items()
        },
        "malformed": False,
        "gate_blocked": False,
    }


def _verified(_review, _request):
    return True


class GateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "frozen"
        self.source.mkdir()
        for chapter in range(1, 7):
            (self.source / f"chapter_{chapter:04d}.txt").write_text(
                _text(chapter), encoding="utf-8"
            )
        self.shadow = self.root / "shadow"

    def tearDown(self):
        self.temp.cleanup()

    def _rewrite(self, request):
        return {
            chapter: _text(chapter, request.round_number)
            for chapter in request.rebuild_chapters
        }

    def test_missing_gate_fields_fail_closed(self):
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "valid current"):
            gate_migration.locate_problem_chapters(
                {"status": "FAIL", "action": "PAUSE", "problem_chapters": [2]}, 6
            )

    def test_empty_problem_chapters_refuses_blind_rewrite(self):
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "blind rewrite"):
            gate_migration.locate_problem_chapters(_failed(), 6)

    def test_legacy_commercial_result_extracts_problem_chapters(self):
        result = {
            "status": "FAIL",
            "current": "FAIL",
            "action": "PAUSE",
            "raw": "RISKS:\n- 第5章重复\n- 第2章时间线错误",
        }
        self.assertEqual((2, 5), gate_migration.locate_problem_chapters(result, 6))

    def test_out_of_range_problem_chapter_fails_closed(self):
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "outside"):
            gate_migration.plan_suffix_rebuild(_failed(2, 7), 6, round_number=1)

    def test_earliest_problem_rebuilds_continuous_suffix(self):
        plan = gate_migration.plan_suffix_rebuild(_failed(5, 2, 5), 6, round_number=1)
        self.assertEqual(2, plan["earliest_problem_chapter"])
        self.assertEqual([2, 3, 4, 5, 6], plan["rebuild_chapters"])
        self.assertEqual([2, 5], plan["problem_chapters"])

    def test_discontinuous_frozen_source_is_rejected(self):
        (self.source / "chapter_0004.txt").unlink()
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "discontinuous"):
            gate_migration.snapshot_frozen_source(self.source, 6)

    def test_real_chinese_named_frozen_source_is_continuous(self):
        chinese_source = self.root / "中文冻结源"
        chinese_source.mkdir()
        for chapter in range(1, 7):
            (chinese_source / f"第{chapter:04d}章.txt").write_text(
                _text(chapter), encoding="utf-8"
            )
        snapshot = gate_migration.snapshot_frozen_source(chinese_source, 6)
        self.assertEqual(tuple(range(1, 7)), tuple(snapshot.paths))
        self.assertEqual("第0001章.txt", snapshot.paths[1].name)
        self.assertEqual(64, len(snapshot.snapshot_sha256))

    def test_mixed_names_for_same_chapter_are_rejected_as_duplicate(self):
        (self.source / "第0003章.txt").write_text(_text(3), encoding="utf-8")
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "duplicate"):
            gate_migration.snapshot_frozen_source(self.source, 6)

    def test_source_sha_drift_during_rebuild_fails_closed(self):
        def tampering_rebuild(request):
            (self.source / "chapter_0001.txt").write_text("tampered", encoding="utf-8")
            return self._rewrite(request)

        with self.assertRaisesRegex(gate_migration.GateMigrationError, "SHA drift"):
            gate_migration.run_gate_migration(
                gate_result=_failed(3),
                gate_chapter=6,
                frozen_source=self.source,
                shadow_root=self.shadow,
                rebuild_fn=tampering_rebuild,
                full_review_fn=lambda request: _review_result(request, passed=True),
                review_verifier_fn=_verified,
            )
        self.assertFalse(any(self.shadow.rglob("chapter_*.txt")))

    def test_second_round_failure_returns_non_finalizable_plan(self):
        calls = []
        rebuild_requests = []

        def capture_rewrite(request):
            rebuild_requests.append(request)
            return self._rewrite(request)

        def always_fail(request):
            calls.append(request.round_number)
            return _review_result(request, passed=False, problems=(4,))

        plan = gate_migration.run_gate_migration(
            gate_result=_failed(2),
            gate_chapter=6,
            frozen_source=self.source,
            shadow_root=self.shadow,
            rebuild_fn=capture_rewrite,
            full_review_fn=always_fail,
            review_verifier_fn=_verified,
        )
        self.assertEqual([1, 2], calls)
        self.assertEqual(2, len(rebuild_requests))
        second = rebuild_requests[1]
        self.assertIn("修订轮次1", second.source_texts[4])
        self.assertEqual(
            gate_migration._text_sha256(second.source_texts[4]),
            second.source_chapter_hashes[4],
        )
        self.assertNotEqual(
            second.source_chapter_hashes[4], second.frozen_chapter_hashes[4]
        )
        self.assertEqual(
            rebuild_requests[0].frozen_chapter_hashes,
            second.frozen_chapter_hashes,
        )
        self.assertEqual(
            rebuild_requests[0].frozen_snapshot_sha256,
            second.frozen_snapshot_sha256,
        )
        self.assertEqual("BLOCKED", plan["status"])
        self.assertFalse(plan["finalize_allowed"])
        self.assertEqual("", plan["final_shadow_dir"])
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "not ready"):
            gate_migration.validate_finalize_plan(plan)

    def test_success_plan_requires_exact_full_range_review_and_validates(self):
        requests = []

        def pass_review(request):
            requests.append(request)
            return _review_result(request, passed=True)

        plan = gate_migration.run_gate_migration(
            gate_result=_failed(4, 2),
            gate_chapter=6,
            frozen_source=self.source,
            shadow_root=self.shadow,
            rebuild_fn=self._rewrite,
            full_review_fn=pass_review,
            review_verifier_fn=_verified,
        )
        self.assertEqual("READY_TO_FINALIZE", plan["status"])
        self.assertTrue(plan["finalize_allowed"])
        self.assertEqual([2, 3, 4, 5, 6], plan["rounds"][0]["rebuild_chapters"])
        self.assertEqual(tuple(range(1, 7)), requests[0].expected_chapters)
        final_candidate = Path(plan["final_shadow_dir"])
        self.assertEqual(6, len(list(final_candidate.glob("chapter_*.txt"))))
        self.assertEqual(_text(1), (final_candidate / "chapter_0001.txt").read_text(encoding="utf-8"))
        self.assertEqual(64, len(plan["final_review_sha256"]))
        self.assertEqual(
            plan["final_review_sha256"], plan["rounds"][-1]["full_review_sha256"]
        )
        self.assertTrue(gate_migration.validate_finalize_plan(plan, _verified))
        self.assertEqual(64, len(plan["plan_sha256"]))

    def test_incomplete_full_review_never_produces_finalize_plan(self):
        def incomplete_review(request):
            result = _review_result(request, passed=True)
            result["reviewed_chapters"] = list(range(2, 7))
            return result

        with self.assertRaisesRegex(gate_migration.GateMigrationError, "every chapter"):
            gate_migration.run_gate_migration(
                gate_result=_failed(3),
                gate_chapter=6,
                frozen_source=self.source,
                shadow_root=self.shadow,
                rebuild_fn=self._rewrite,
                full_review_fn=incomplete_review,
                review_verifier_fn=_verified,
            )

    def test_finalize_validation_detects_late_frozen_sha_drift(self):
        plan = gate_migration.run_gate_migration(
            gate_result=_failed(6),
            gate_chapter=6,
            frozen_source=self.source,
            shadow_root=self.shadow,
            rebuild_fn=self._rewrite,
            full_review_fn=lambda request: _review_result(request, passed=True),
            review_verifier_fn=_verified,
        )
        (self.source / "chapter_0002.txt").write_text("late drift", encoding="utf-8")
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "changed"):
            gate_migration.validate_finalize_plan(plan, _verified)

    def test_shadow_parent_symlink_is_rejected_before_creation(self):
        linked_parent = self.root / "linked-parent"
        linked_parent.mkdir()
        real_is_symlink = Path.is_symlink

        def simulate_parent_symlink(path):
            if path == linked_parent:
                return True
            return real_is_symlink(path)

        with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=simulate_parent_symlink):
            with self.assertRaisesRegex(gate_migration.GateMigrationError, "symlink"):
                gate_migration._prepare_shadow_root(
                    self.source, linked_parent / "new-shadow"
                )

    def test_resolved_shadow_containment_is_rejected(self):
        disguised = self.source / "nested" / ".." / "shadow"
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "isolated"):
            gate_migration._prepare_shadow_root(self.source, disguised)

    def test_forged_pass_is_rejected_without_independent_verification(self):
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "explicit True"):
            gate_migration.run_gate_migration(
                gate_result=_failed(3),
                gate_chapter=6,
                frozen_source=self.source,
                shadow_root=self.shadow,
                rebuild_fn=self._rewrite,
                full_review_fn=lambda request: _review_result(request, passed=True),
                review_verifier_fn=lambda _review, _request: False,
            )

    def test_missing_or_raising_review_verifier_fails_closed(self):
        common = {
            "gate_result": _failed(3),
            "gate_chapter": 6,
            "frozen_source": self.source,
            "rebuild_fn": self._rewrite,
            "full_review_fn": lambda request: _review_result(request, passed=True),
        }
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "required"):
            gate_migration.run_gate_migration(
                **common, shadow_root=self.root / "missing-verifier"
            )

        def raising_verifier(_review, _request):
            raise ValueError("forged receipt")

        with self.assertRaisesRegex(gate_migration.GateMigrationError, "raised an error"):
            gate_migration.run_gate_migration(
                **common,
                shadow_root=self.root / "raising-verifier",
                review_verifier_fn=raising_verifier,
            )

    def test_finalize_replays_independent_review_verifier(self):
        plan = gate_migration.run_gate_migration(
            gate_result=_failed(3),
            gate_chapter=6,
            frozen_source=self.source,
            shadow_root=self.shadow,
            rebuild_fn=self._rewrite,
            full_review_fn=lambda request: _review_result(request, passed=True),
            review_verifier_fn=_verified,
        )
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "explicit True"):
            gate_migration.validate_finalize_plan(
                plan, lambda _review, _request: False
            )
        with self.assertRaisesRegex(gate_migration.GateMigrationError, "required"):
            gate_migration.validate_finalize_plan(plan)

        def raising_verifier(_review, _request):
            raise RuntimeError("receipt unavailable")

        with self.assertRaisesRegex(gate_migration.GateMigrationError, "raised an error"):
            gate_migration.validate_finalize_plan(plan, raising_verifier)


if __name__ == "__main__":
    unittest.main()
