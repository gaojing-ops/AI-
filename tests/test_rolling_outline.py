import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")
    openai_stub.OpenAI = object
    sys.modules["openai"] = openai_stub
if "jieba" not in sys.modules:
    jieba_stub = types.ModuleType("jieba")
    jieba_stub.lcut = lambda text: list(text)
    sys.modules["jieba"] = jieba_stub

import gui_app


class RollingOutlinePersistenceTests(unittest.TestCase):
    def test_appending_a_new_batch_preserves_previous_batches_and_archives_each(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dirs = dict(gui_app.generator.DIRS)
            dirs["plot"] = str(root / "plot")
            dirs["hist"] = str(root / "history")
            app = object.__new__(gui_app.NovelGeneratorGUI)

            with patch.object(gui_app.generator, "DIRS", dirs):
                app._append_rolling_outline_batch(11, 20, "第11章 第一批\n核心事件：A")
                app._append_rolling_outline_batch(21, 30, "第21章 第二批\n核心事件：B")

            active = (root / "plot" / "AI滚动逐章细纲.txt").read_text(encoding="utf-8")
            archives = list((root / "history" / "outline_archive").glob("outline_*.txt"))

            self.assertIn("第11章 第一批", active)
            self.assertIn("第21章 第二批", active)
            self.assertEqual(2, len(archives))


if __name__ == "__main__":
    unittest.main()
