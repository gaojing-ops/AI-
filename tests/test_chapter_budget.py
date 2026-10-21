import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import chapter_budget as cb
import gui_app
import repair_engine


class ChapterBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def budget(self, **kwargs):
        return cb.ChapterBudget(self.root, 7, **kwargs)

    def test_restart_and_config_increase_cannot_refund_attempts(self):
        budget = self.budget(calls=2)
        budget.transact('call', now=10)
        restarted = self.budget(calls=20)
        restarted.transact('call', now=11)
        with self.assertRaisesRegex(cb.BudgetStopped, '总调用'):
            restarted.transact('call', now=12)
        self.assertEqual(2, restarted.transact()['calls'])
        self.assertEqual(2, restarted.transact()['call_limit'])

    def test_deadline_survives_restart_and_latches(self):
        self.budget(seconds=10).transact('call', now=100)
        with self.assertRaisesRegex(cb.BudgetStopped, '总时限'):
            self.budget(seconds=100).transact('check', now=111)
        with self.assertRaises(cb.BudgetStopped):
            self.budget().transact('call', now=105)

    def test_clock_rollback_stops(self):
        self.budget().transact('call', now=100)
        with self.assertRaisesRegex(cb.BudgetStopped, '时钟回退'):
            self.budget().transact('call', now=99)

    def test_one_repair_is_shared_across_stages_and_restart(self):
        self.budget().transact('repair', now=100)
        with self.assertRaisesRegex(cb.BudgetStopped, '修复一轮'):
            self.budget().transact('repair', now=101)
        self.assertEqual(1, self.budget().transact()['repairs'])

    def test_explicit_renew_preserves_old_epoch(self):
        budget = self.budget()
        old = budget.transact('call', now=10)
        budget.renew()
        new = budget.transact()
        self.assertEqual(0, new['calls'])
        self.assertNotEqual(old['epoch'], new['epoch'])
        archived = budget.path.with_name(budget.path.stem + '.' + old['epoch'] + '.json')
        self.assertEqual(old, json.loads(archived.read_text(encoding='utf-8')))

    def test_corrupt_record_stops_not_reset(self):
        budget = self.budget()
        budget.transact('call', now=10)
        budget.path.write_text('{bad', encoding='utf-8')
        with self.assertRaisesRegex(cb.BudgetStopped, '损坏'):
            self.budget().transact('call', now=11)
        self.assertEqual('{bad', budget.path.read_text(encoding='utf-8'))

    def test_disk_failure_blocks_before_reservation(self):
        budget = self.budget()
        with mock.patch.object(budget, '_write', side_effect=OSError('disk')):
            with self.assertRaisesRegex(cb.BudgetStopped, '无法持久保存'):
                budget.transact('call', now=10)

    def test_other_process_lock_never_auto_retried(self):
        budget = self.budget()
        budget.path.parent.mkdir(parents=True)
        budget.path.with_suffix('.lock').touch()
        with self.assertRaisesRegex(cb.BudgetStopped, '占用'):
            budget.transact('call')
        self.assertFalse(budget.path.exists())

    def test_separate_chapter_gets_own_budget(self):
        self.budget().transact('call', now=10)
        self.assertEqual(0, cb.ChapterBudget(self.root, 8).transact()['calls'])

    def test_stream_chunks_do_not_repeatedly_read_or_write_disk(self):
        budget = self.budget(seconds=100)
        budget.transact('call', now=100)
        with mock.patch.object(cb.time, 'time', return_value=110):
            self.assertEqual(90, budget.remaining(120))
            with mock.patch.object(budget, '_read') as read, mock.patch.object(budget, '_write') as write:
                for _ in range(1000):
                    budget.check_stream()
                read.assert_not_called()
                write.assert_not_called()
        with mock.patch.object(cb.time, 'time', return_value=201):
            with self.assertRaises(cb.BudgetStopped):
                budget.check_stream()

    def test_stream_is_closed_on_budget_stop(self):
        app = self.make_app()
        app.get_model_provider = lambda: gui_app.PROVIDER_DEEPSEEK
        app._durable_chapter_budget = mock.Mock()
        app._durable_chapter_budget.check_stream.side_effect = cb.BudgetStopped('limit')
        app._deepseek_request_kwargs = lambda **kwargs: {}
        stream = mock.MagicMock()
        stream.__iter__.return_value = iter([object()])
        app.get_client = mock.Mock()
        app.get_client.return_value.chat.completions.create.return_value = stream
        with self.assertRaises(cb.BudgetStopped):
            app._call_generation_text('system', 'user', model_name='fake', token_limit=10, task_type='test')
        stream.close.assert_called_once()

    def make_app(self):
        app = gui_app.NovelGeneratorGUI.__new__(gui_app.NovelGeneratorGUI)
        app.config = {'max_model_calls_per_chapter': 4, 'model_usage_ledger_enabled': False,
                      'structured_state_enabled': False}
        app.project_dir = self.root
        app._model_call_lock = threading.Lock()
        app._model_call_count = app._model_call_limit = 0
        app._ui = lambda callback: None
        app._configured_cost_limit_cny = lambda: 0
        return app

    def test_gui_resume_and_new_process_keep_count(self):
        app = self.make_app()
        app._begin_chapter_model_budget(7)
        app._before_model_call()
        app._begin_chapter_model_budget(7)
        self.assertEqual(1, app._chapter_model_call_count)
        restarted = self.make_app()
        restarted._begin_chapter_model_budget(7)
        self.assertEqual(1, restarted._chapter_model_call_count)
        for _ in range(3):
            restarted._before_model_call()
        with self.assertRaises(cb.BudgetStopped):
            restarted._before_model_call()
        self.assertEqual(4, self.budget().transact()['calls'])

    def test_prose_and_state_share_one_repair(self):
        app = self.make_app()
        app._begin_chapter_model_budget(7)
        app._claim_chapter_repair()
        app._begin_chapter_model_budget(7)
        with self.assertRaises(cb.BudgetStopped):
            app._claim_chapter_repair()

    def test_deepseek_client_disables_hidden_retry(self):
        app = self.make_app()
        app.get_model_provider = lambda: gui_app.PROVIDER_DEEPSEEK
        app._active_model_preset = lambda: {'base_url': 'https://api.deepseek.com'}
        app._get_runtime_api_key = lambda: 'fake-test-key'
        with mock.patch.object(gui_app, 'OpenAI') as client:
            app.get_client()
        self.assertEqual(0, client.call_args.kwargs['max_retries'])

    def test_local_report_collects_all_and_does_not_call_models(self):
        app = self.make_app()
        app.config['canon_guard_enabled'] = False
        app._detect_chapter_repetition = lambda *args: ['重复段落']
        app._check_content_drift = lambda *args, **kwargs: ['禁词']
        app._run_semantic_consistency_guard = lambda *args: {'passed': True}
        app.call_llm_review = mock.Mock(side_effect=AssertionError('no model'))
        report = {'passed': False, 'status': 'fail', 'issues': [
            {'message': '短章', 'blocking': True}, {'message': '措辞建议', 'blocking': False}]}
        with mock.patch.object(gui_app.chapter_validator, 'validate_candidate', return_value=report) as validate:
            result = app._collect_local_review('第7章 旧标题\n' + '似乎' * 5, 7, {}, {'旧标题'})
        validate.assert_called_once()
        self.assertEqual(5, len(result['issues']))
        self.assertEqual(['措辞建议'], result['warnings'])
        app.call_llm_review.assert_not_called()

    def test_service_errors_never_trigger_prose_rewrite(self):
        for reason in ('质检服务调用失败', 'CHAPTER_BUDGET_STOPPED', '质检裸奔', 'timeout'):
            with self.subTest(reason=reason):
                self.assertFalse(repair_engine.should_rewrite(reason, '正文' * 1800))
                self.assertEqual('pause', repair_engine.decide_after_retries(reason, '正文' * 1800)['action'])

    def test_attempts_are_clamped_to_initial_plus_one(self):
        for value, expected in [(99, 2), (3, 2), (1, 1), ('bad', 2), (None, 2)]:
            self.assertEqual(expected, cb.bounded_attempts(value))

    def test_isolated_app_uses_original_project_budget(self):
        app = self.make_app()
        app._begin_chapter_model_budget(7)
        app._before_model_call()
        isolated = self.make_app()
        isolated.project_dir = str(Path(self.root) / 'sandbox')
        isolated._chapter_budget_project = self.root
        isolated._begin_chapter_model_budget(7)
        isolated._before_model_call()
        self.assertEqual(2, self.budget().transact()['calls'])
        self.assertFalse(Path(isolated.project_dir).exists())

    def test_budget_record_rejects_unsafe_epoch_and_negative_count(self):
        budget = self.budget()
        row = budget.transact('call', now=10)
        for patch in ({'epoch': '../escape'}, {'calls': -1}, {'calls': True}, {'repairs': 2}):
            with self.subTest(patch=patch):
                budget.path.write_text(json.dumps({**row, **patch}), encoding='utf-8')
                with self.assertRaisesRegex(cb.BudgetStopped, '损坏'):
                    budget.transact('call', now=11)

    def test_budget_module_is_in_code_fingerprint(self):
        app = self.make_app()
        with mock.patch.object(gui_app.formal_suffix_rewrite, 'build_preflight_fingerprint', return_value='fp') as build:
            app._formal_suffix_code_fingerprint()
        self.assertNotEqual('missing', build.call_args.args[0]['chapter_budget.py'])
