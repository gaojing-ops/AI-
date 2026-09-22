import unittest

import book_initializer


class SemanticInvariantPromptTests(unittest.TestCase):
    def test_prompt_requires_recap_evidence_details(self):
        _system, user = book_initializer.build_semantic_invariants_prompt(
            "测试书", "职业悬疑", "世界", "人物", "唯一真相"
        )
        for term in ("时间", "时长", "数量", "证据介质", "删除/保全状态"):
            self.assertIn(term, user)


if __name__ == "__main__":
    unittest.main()
