import unittest
from unittest.mock import patch
import sys
import types

if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")
    openai_stub.OpenAI = object
    sys.modules["openai"] = openai_stub
if "jieba" not in sys.modules:
    jieba_stub = types.ModuleType("jieba")
    jieba_stub.lcut = lambda text: list(text)
    sys.modules["jieba"] = jieba_stub

import gui_app


class _DummyGUI:
    def __init__(self, state, max_cycles=6):
        self.is_batch_running = False
        self.config = {
            "auto_resume_full_book": True,
            "auto_resume_network_max_cycles": max_cycles,
            "health_check_future_chapters": 30,
        }
        self.state = dict(state)
        self.writes = []
        self.started = []

    def _load_batch_state(self):
        return dict(self.state)

    def _write_batch_state(self, **updates):
        self.state.update(updates)
        self.writes.append(updates)

    def _run_health_check_internal(self, future_window):
        return []

    def _start_batch_job(self, count, label, target_end, resume):
        self.started.append((count, label, target_end, resume))


class AutoResumeTests(unittest.TestCase):
    def test_network_resume_stops_at_configured_cycle_limit(self):
        dummy = _DummyGUI({
            "mode": "full_book",
            "status": "interrupted",
            "resume_allowed": True,
            "network_resume_cycles": 6,
            "target_end": 40,
        })

        gui_app.NovelGeneratorGUI._auto_resume_interrupted_full_book(dummy)

        self.assertEqual([], dummy.started)
        self.assertEqual("paused", dummy.state["status"])
        self.assertFalse(dummy.state["resume_allowed"])

    def test_network_resume_continues_from_next_unsaved_chapter(self):
        dummy = _DummyGUI({
            "mode": "full_book",
            "status": "interrupted",
            "resume_allowed": True,
            "network_resume_cycles": 2,
            "target_end": 40,
        })
        latest = ("第一卷", 31, "unused", 30, "unused")

        with patch.object(gui_app.generator, "get_latest_chapter_info", return_value=latest):
            gui_app.NovelGeneratorGUI._auto_resume_interrupted_full_book(dummy)

        self.assertEqual([(10, "一键写完整本（自动恢复）", 40, True)], dummy.started)

    def test_review_streak_counts_only_immediate_tail(self):
        dummy = types.SimpleNamespace()
        dummy._load_latest_chapter_statuses = lambda: {
            27: {"status": "可继续但需复核"},
            28: {"status": "正式可用"},
            29: {"status": "可继续但需复核"},
            30: {"status": "可继续但需复核"},
        }
        latest = (1, 31, "unused", 30, "unused")
        with patch.object(gui_app.generator, "get_latest_chapter_info", return_value=latest):
            count = gui_app.NovelGeneratorGUI._count_consecutive_review_chapters(dummy)
        self.assertEqual(2, count)


if __name__ == "__main__":
    unittest.main()
