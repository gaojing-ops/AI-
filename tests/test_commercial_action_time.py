"""A rewound manuscript must not inherit future-stage action requirements."""
import unittest
from unittest import mock
from types import SimpleNamespace

from gui_app import NovelGeneratorGUI


class CommercialActionTimeTests(unittest.TestCase):
    def render(self, latest, text):
        app = SimpleNamespace(config={"revision_debt_enabled": False}, latest_chap=latest,
                              _load_commercial_blueprint=lambda: {})
        with mock.patch("gui_app.generator.read_text_safe", return_value=text):
            return NovelGeneratorGUI._commercial_contract_context(app)

    def test_future_action_board_does_not_leak_after_rewind(self):
        self.assertEqual("", self.render(2, "# 第87章后商业行动单\n第88—90章必须作选择"))

    def test_current_board_remains_active(self):
        self.assertIn("必须作选择", self.render(89, "# 第87章后商业行动单\n必须作选择"))

    def test_no_header_legacy_board_remains_available(self):
        self.assertIn("当前行动", self.render(2, "当前行动"))

    def test_future_board_header_with_spaces(self):
        self.assertEqual("", self.render(2, "# 第 87 章后商业行动单\n未来行动"))


if __name__ == "__main__":
    unittest.main()
