import json
import tempfile
import unittest
from pathlib import Path

import state_ledger


def test_extraction_prompt_requires_evidenced_artifact_content_updates():
    system, _ = state_ledger.build_extraction_prompts(74, '周予安在折纸背面写下观察。')
    assert '批注、改写、签署、转交或归还' in system
    assert 'resources' in system and 'status' in system
    assert '沿用已有item' in system
    assert '不得把内容修改记成新增一份物品' in system


def test_extraction_prompt_distinguishes_sparse_fields_from_missing_facts():
    system, _ = state_ledger.build_extraction_prompts(75, '周予安接过材料说明。')
    assert '宁可少填没有证据的字段' in system
    assert '不得漏掉已有连续原文支持的关键变化' in system


def test_extraction_prompt_records_evidenced_final_real_location():
    system, _ = state_ledger.build_extraction_prompts(78, '周予安站在康桥报名处。')
    assert '章末仍有效的现实地点' in system
    assert 'characters.location' in system
    assert '计划、回忆、梦境或灰影' in system


def test_audit_prompt_checks_missing_real_location_without_inventing_movement():
    system, prompt = state_ledger.build_delta_audit_prompts({'characters': []})
    assert '人物现实地点变化' in system
    assert 'characters、resources或events' in prompt
    assert '事件摘要不能替代characters.location' in prompt
    assert '计划、回忆、梦境或灰影' in prompt


def test_extraction_records_unnamed_but_explicit_current_position():
    system, _ = state_ledger.build_extraction_prompts(85, '林舟站在球场的中场线后。')
    assert '地点不要求专有场馆名' in system
    assert '单独增加地点条目' in system
    assert '不能补造城市、场馆名或未经描写的移动路线' in system


def test_audit_checks_inherited_location_when_delta_fields_are_empty():
    system, _ = state_ledger.build_delta_audit_prompts({'characters': []})
    assert '只有NO_CHANGE配空location才会继承前章旧地点' in system
    assert 'UNKNOWN清除陈旧地点' in system
    assert '地点不要求专有场馆名' in system
    assert 'missing' in system
    assert '不能补造城市、场馆名或未经描写的移动路线' in system


def test_state_prompts_expose_unknown_location_without_inventing_destination():
    extraction = state_ledger.build_extraction_prompts(
        84, '周予安背起包，跟着杨铭走了出去。'
    )
    audit = state_ledger.build_delta_audit_prompts(
        {'characters': []},
        chapter_text='周予安背起包，跟着杨铭走了出去。',
    )
    for system, prompt in (extraction, audit):
        combined = system + prompt
        assert 'location_state' in combined
        assert 'UNKNOWN' in combined
        assert 'NO_CHANGE' in combined
        assert '旧地点已失效' in combined
        assert '最终位置没有明确写出' in combined


def test_both_state_prompts_prefer_stable_scene_over_departed_spot():
    prompts = [state_ledger.build_extraction_prompts(85, '林舟转身回追。'),
               state_ledger.build_delta_audit_prompts({})]
    for pair in prompts:
        system = pair[0]
        assert '球场中场线后、车厢内' not in system
        assert '已离开的瞬时站位不作章末地点' in system
        assert '不能仅因是人物最后一次出场就写成章末SET' in system
        assert '有连续原文支持的较粗场景' in system
        assert '不能通过NO_CHANGE继承已失效的旧地点' in system


def test_both_state_prompts_audit_supporting_characters_not_only_delta_names():
    text = '林舟在家等候。父亲林海回到家，把钥匙放在餐桌上。'
    extraction = state_ledger.build_extraction_prompts(87, text)
    audit = state_ledger.build_delta_audit_prompts(
        {'characters': [{'name': '林舟', 'location': '家'}]}, chapter_text=text
    )
    for prompts in (extraction, audit):
        system = prompts[0]
        assert '逐一核对本章实际出场人物，包括父母、教练等配角' in system
        assert '不得只核对主角或delta已有姓名' in system
        assert '配角地点有本章明确证据时也须单列' in system
        assert '称谓须结合已验证关系消歧，不得猜认身份' in system


def test_both_state_prompts_distinguish_audience_from_narrative_flashback():
    text = '林舟对母亲说回复没变。旁白回顾他昨天被要求十七日前答复。父亲摆好筷子。'
    for prompts in (state_ledger.build_extraction_prompts(88, text),
                    state_ledger.build_delta_audit_prompts({}, chapter_text=text)):
        combined = ''.join(prompts)
        assert '在场、摆筷子或相邻段落出现不等于听到' in combined
        assert '后接旁白回顾不得自动扩成向同场其他角色复述全部历史内容' in combined
        assert '明确写出某人亲历、听到、读到或被告知' in combined
        assert '不能把所有叙述句一概排除' in combined


def test_both_state_prompts_keep_sending_separate_from_recipient_knowledge():
    for text in (
        '林舟把报名结果发给任安。',
        '林舟把核准通知转发给母亲。',
        '林舟把日程群发给队友。',
    ):
        pairs = (state_ledger.build_extraction_prompts(1, text),
                 state_ledger.build_delta_audit_prompts({}, chapter_text=text))
        for system, prompt in pairs:
            assert '发送、转发或群发消息只证明发送动作' in system
            assert '不能据此认定收件人已经收到、读到或获知内容' in system
            assert text in prompt


def test_both_state_prompts_keep_third_party_acknowledgement_with_its_actor():
    text = '林舟把结果发给父母，又给任安回了收到。'
    pairs = (state_ledger.build_extraction_prompts(1, text),
             state_ledger.build_delta_audit_prompts({}, chapter_text=text))
    for system, _ in pairs:
        assert '发信人向第三方回复“收到”，不是其他收件人的接收确认' in system
        assert '逐一核对目标角色与同一具体信息' in system


def test_both_state_prompts_allow_explicit_recipient_narration_without_dialogue_ack():
    text = '任安读到林舟转发的核准通知，得知报名已通过。'
    pairs = (state_ledger.build_extraction_prompts(1, text),
             state_ledger.build_delta_audit_prompts({}, chapter_text=text))
    for system, prompt in pairs:
        assert '明确写出某人亲历、听到、读到或被告知' in system
        assert '不强制要求对话回执' in system
        assert '发送动作本身仍可按原文记入events' in system
        assert text in prompt


def test_sending_event_does_not_synthesize_recipient_knowledge():
    text = '林舟把报名结果发给任安。'
    delta, issues = state_ledger.validate_delta({
        'chapter': 1,
        'events': [{'event_type': 'other', 'summary': '林舟发送报名结果',
                    'evidence_quote': text}],
    }, 1, text, state_ledger.initial_state())
    assert issues == []
    result = state_ledger._apply_delta_to_state(
        state_ledger.initial_state(), delta, state_ledger.chapter_sha256(text)
    )
    assert result['current_chapter'] == 1
    assert '任安' not in result['characters']


def test_explicit_recipient_knowledge_remains_materialized():
    text = '任安读到林舟转发的核准通知，得知报名已通过。'
    delta, issues = state_ledger.validate_delta({
        'chapter': 1,
        'characters': [{'name': '任安', 'knowledge_add': ['报名已通过'],
                        'evidence_quote': text}],
        'events': [{'event_type': 'reveal', 'summary': '任安读到核准通知',
                    'evidence_quote': text}],
    }, 1, text, state_ledger.initial_state())
    assert issues == []
    result = state_ledger._apply_delta_to_state(
        state_ledger.initial_state(), delta, state_ledger.chapter_sha256(text)
    )
    assert '报名已通过' in result['characters']['任安']['knowledge']


def test_send_only_quote_cannot_materialize_recipient_knowledge():
    text = (
        '车开上回家的路，手机屏幕在这时亮起。赵启明发来次日集合时间和地点，'
        '消息里没有十八人名单。周予安低头看了几秒。'
    )
    _delta, issues = state_ledger.validate_delta({
        'chapter': 75,
        'characters': [{
            'name': '周予安',
            'knowledge_add': ['次日集合时间和地点；通知没有十八人名单'],
            'evidence_quote': text,
        }],
        'events': [{'event_type': 'other', 'summary': '赵启明发送次日集合通知',
                    'evidence_quote': text}],
    }, 75, text, state_ledger.initial_state())
    assert any('仅有发送动作证据' in issue for issue in issues)


def test_message_look_followed_by_content_comparison_without_object_is_still_send_only():
    text = (
        '手机屏幕亮起。赵启明发来次日集合时间和地点，消息里没有十八人名单。'
        '周予安低头看了几秒。训练本写着“通过”，临江的通知却只给了集合时间和地点。'
    )
    _delta, issues = state_ledger.validate_delta({
        'chapter': 75,
        'characters': [{
            'name': '周予安',
            'knowledge_add': ['次日集合时间和地点；通知没有十八人名单'],
            'evidence_quote': text,
        }],
        'events': [{'event_type': 'reveal', 'summary': '周予安查看集合通知',
                    'evidence_quote': text}],
    }, 75, text, state_ledger.initial_state())
    assert any('仅有发送动作证据' in issue for issue in issues)


def test_explicit_message_read_still_supports_recipient_knowledge():
    text = '赵启明发来次日集合通知。周予安点开消息，读到集合时间是八点。'
    _delta, issues = state_ledger.validate_delta({
        'chapter': 75,
        'characters': [{
            'name': '周予安',
            'knowledge_add': ['集合时间是八点'],
            'evidence_quote': text,
        }],
        'events': [{'event_type': 'reveal', 'summary': '周予安读到集合时间',
                    'evidence_quote': text}],
    }, 75, text, state_ledger.initial_state())
    assert issues == []


def test_explicit_message_kanwan_still_supports_recipient_knowledge():
    text = (
        '罗竞很快回了一句：“知道了，别往我这边赶。”'
        '过了片刻，又发来：“踢完报比分。”周予安看完，把手机收进包里。'
    )
    _delta, issues = state_ledger.validate_delta({
        'chapter': 84,
        'characters': [{
            'name': '周予安',
            'knowledge_add': ['罗竞要求他不用赶往选拔处，并在踢完后报告比分'],
            'evidence_quote': text,
        }],
        'events': [{'event_type': 'bond', 'summary': '周予安看完罗竞回复',
                    'evidence_quote': text}],
    }, 84, text, state_ledger.initial_state())
    assert issues == []


def test_audit_cannot_turn_send_only_quote_into_missing_knowledge():
    text = '赵启明发来次日集合时间和地点。周予安低头看了几秒。'
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': '漏记周予安新获知次日集合时间和地点',
            'evidence_quote': text,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, text)
    assert issues == []


def test_audit_keeps_missing_knowledge_when_recipient_explicitly_reads():
    text = '赵启明发来次日集合通知。周予安点开消息，读到集合时间是八点。'
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': '漏记周予安新获知集合时间是八点',
            'evidence_quote': text,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, text)
    assert len(issues) == 1
    assert issues[0].startswith('characters提取遗漏：')


def test_audit_ignores_precise_location_missing_when_character_moves_later():
    quote = '评价场只用了半片区域。韩立新站在边线外，脚边是记录夹。'
    text = quote + '评价结束后，韩立新走到电脑旁完成登记，又从打印机旁取出副页。'
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': '漏记韩立新本章明确的现实位置：康桥评价场边线外',
            'evidence_quote': quote,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, text)
    assert issues == []


def test_audit_ignores_location_missing_when_same_quote_shows_departure():
    quote = (
        '贺子骁就在场地另一端收球。下一只球滚到脚边，他把球推到筐旁，'
        '转身去追最后一只。'
    )
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': '漏记贺子骁本章明确的现实位置：评价场场地另一端',
            'evidence_quote': quote,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, quote)
    assert issues == []


def test_audit_ignores_chapter_end_location_wording_when_same_quote_shows_departure():
    quote = (
        '贺子骁就在场地另一端收球。下一只球滚到脚边，他抬头看了眼器材筐，'
        '右脚将球推到筐旁，转身去追最后一只。'
    )
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': '漏记贺子骁章末仍有效的现实地点：康桥评价场内（场地另一端）',
            'evidence_quote': quote,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, quote)
    assert issues == []


def test_audit_ignores_broad_location_missing_not_named_in_quote():
    quote = '电脑旁，韩立新先调出冬训记录，又打开两场资格赛的完整文件。'
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': (
                '漏记韩立新仍在康桥评价场这一有连续证据支持的现实地点；'
                '应单列location_state=SET，地点可最小记录为“康桥评价场”'
            ),
            'evidence_quote': quote,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, quote)
    assert issues == []


def test_audit_ignores_inferred_staff_location_not_named_in_quote():
    quote = (
        '一名工作人员递给周予安一份后续申报材料说明。'
        '工作人员收起其余表格，招呼下一名队员过来。'
    )
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': (
                '漏记该工作人员仍在康桥评价场并继续办理下一名队员事务的现实地点；'
                '可用原文称谓“一名工作人员”单列location_state=SET，'
                '地点可最小记录为“康桥评价场”'
            ),
            'evidence_quote': quote,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, quote)
    assert issues == []


def test_audit_keeps_missing_location_when_quote_is_final_and_stable():
    text = '十分钟后车停到路边。周予安上车，坐在车内。车开上回家的路。'
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [{
            'category': 'characters',
            'reason': '漏记周予安本章明确的现实位置：车内',
            'evidence_quote': text,
        }],
        'reject': [],
    }, {'chapter': 75, 'characters': []}, text)
    assert len(issues) == 1
    assert issues[0].startswith('characters提取遗漏：')


def test_audit_does_not_treat_sparse_no_change_row_as_overriding_sibling_set():
    text = (
        '十分钟后，车停到路边。周予安上车，把背包放到脚边。'
        '韩立新此前告诉周予安：“评价通过，但不保证比赛分钟。”'
    )
    delta = {
        'chapter': 75,
        'characters': [
            {
                'name': '周予安', 'location': '车内', 'location_state': 'SET',
                'condition': '', 'emotion': '', 'status': '',
                'knowledge_add': [], 'abilities_add': [],
                'evidence_quote': '十分钟后，车停到路边。周予安上车，把背包放到脚边。',
            },
            {
                'name': '周予安', 'location': '', 'location_state': 'NO_CHANGE',
                'condition': '', 'emotion': '', 'status': '',
                'knowledge_add': ['评价通过，但不保证比赛分钟'], 'abilities_add': [],
                'evidence_quote': '韩立新此前告诉周予安：“评价通过，但不保证比赛分钟。”',
            },
        ],
    }
    issues = state_ledger.validate_delta_audit({
        'pass': False,
        'missing': [],
        'reject': [{
            'path': 'characters[1]',
            'reason': '该条的NO_CHANGE会继承前章旧地点，与本章章末车内位置冲突；知识字段有据。',
            'draft_quote': '韩立新此前告诉周予安：“评价通过，但不保证比赛分钟。”',
            'state_reference': '前章地点=临江训练场；本章章末地点=车内',
            'repair_instruction': '保留knowledge_add；统一地点为SET车内。',
        }],
    }, delta, text)
    assert issues == []


def test_audit_prompt_evaluates_character_location_across_all_rows():
    system, _ = state_ledger.build_delta_audit_prompts({'characters': []})
    assert '按同一人物的全部characters条目聚合判断最终地点' in system
    assert 'NO_CHANGE只是该行不修改地点' in system


def test_third_party_ack_does_not_materialize_other_recipient_knowledge():
    text = '周予安把年度注册结果发给父母，又给韩立新回了收到。'
    _delta, issues = state_ledger.validate_delta({
        'chapter': 78,
        'characters': [{
            'name': '彭岚',
            'knowledge_add': ['年度注册已经通过'],
            'evidence_quote': text,
        }],
        'events': [{'event_type': 'other', 'summary': '周予安发送年度注册结果',
                    'evidence_quote': text}],
    }, 78, text, state_ledger.initial_state())
    assert any('仅有发送动作证据' in issue for issue in issues)


def test_audit_missing_claim_must_meet_same_evidence_standard_as_delta():
    system, _ = state_ledger.build_delta_audit_prompts({})
    assert 'missing与已有claim适用同一证据标准' in system
    assert '先自检所要求补入的最小条目能否通过同一连续引文的逐字段审计' in system
    assert '不得先要求补入、再以同一已知证据缺口拒绝' in system


def test_audit_repairs_structured_claim_without_inventing_prose_disclosure():
    system, prompt = state_ledger.build_delta_audit_prompts({})
    assert '增量路径的修复只要求改提取字段或扩大连续引文' in system
    assert '不得为了让错误提取成立而要求正文新增告知' in system
    assert '删除虚假回忆，改为角色首次发现' not in prompt


def test_extraction_prompt_preserves_action_order_and_requires_real_advancement():
    _, prompt = state_ledger.build_extraction_prompts(74, '七号先转肩，再看见罗竞。名单仍未公布。')
    assert '不得倒置先后关系' in prompt
    assert 'GAIN必须有取得或转交动作' in prompt
    assert '仅重申待定、顺位不变' in prompt
    assert '不能登记为ADVANCE' in prompt


def test_both_state_prompts_separate_future_schedule_from_current_discovery():
    extraction = state_ledger.build_extraction_prompts(84, '周予安今天得知十七日选拔。')
    audit = state_ledger.build_delta_audit_prompts({}, chapter_text='周予安今天得知十七日选拔。')
    for prompts in (extraction, audit):
        combined = ''.join(prompts)
        assert '未来活动日期不等于当前获知时间' in combined
        assert 'time_anchor' in combined and '留空' in combined


def test_both_state_prompts_require_consequential_notice_and_new_knowledge():
    extraction = state_ledger.build_extraction_prompts(84, '罗竞收起选拔通知。')
    audit = state_ledger.build_delta_audit_prompts({})
    for prompts in (extraction, audit):
        combined = ''.join(prompts)
        assert '影响后续行动的新通知' in combined
        assert '出发安排' in combined and 'characters.knowledge_add' in combined
        assert '不能只用events代替' in combined
        assert '不要求登记全部闲聊' in combined


def test_both_state_prompts_do_not_turn_casual_feedback_into_required_knowledge():
    extraction = state_ledger.build_extraction_prompts(85, '队友说这球给得早。')
    audit = state_ledger.build_delta_audit_prompts({})
    for prompts in (extraction, audit):
        combined = ''.join(prompts)
        assert '普通赞同、简短评价或对已执行动作的重复反馈不强制记knowledge_add' in combined
        assert '新的指令、约定、限制或影响后续行动的具体信息仍须记录' in combined
        assert '人物评价不能无归因地当成客观事实' in combined


def test_both_state_prompts_require_final_custody_without_parallel_aliases():
    extraction = state_ledger.build_extraction_prompts(84, '程野把目录交给赵启明。')
    audit = state_ledger.build_delta_audit_prompts({})
    for prompts in (extraction, audit):
        combined = ''.join(prompts)
        assert '章末最终保管状态' in combined
        assert '别名' in combined and '仍持有' in combined
        assert '不把保管转移当作所有权转移' in combined


def test_both_state_prompts_require_completed_action_not_scheduled_end():
    extraction = state_ledger.build_extraction_prompts(84, '还剩五分钟，补测到五点十分结束。')
    audit = state_ledger.build_delta_audit_prompts({})
    for prompts in (extraction, audit):
        combined = ''.join(prompts)
        assert '预定结束时间不能证明已经完成' in combined
        assert '交卷、收卷、终场或签署' in combined


def test_both_state_prompts_keep_relative_dates_within_evidence_scope():
    extraction = state_ledger.build_extraction_prompts(84, '罗竞说父亲那天送他去车站。')
    audit = state_ledger.build_delta_audit_prompts({})
    for prompts in (extraction, audit):
        combined = ''.join(prompts)
        assert '相对日期不得凭同章其他段落扩写' in combined
        assert 'knowledge_add' in combined and '连续引文' in combined


class VerifiedProseContextTests(unittest.TestCase):
    def test_only_hashed_formal_predecessors_are_history(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {}
            state = {'applied_chapters': {}}
            for n in (73, 74, 75, 76):
                text = f'第{n}章\n周予安在第{n}章完成动作。'
                path = Path(root) / f'第{n:04d}章.txt'
                path.write_text(text, encoding='utf-8')
                paths[n] = path
                if n != 74:
                    state['applied_chapters'][str(n)] = state_ledger.chapter_sha256(text)
            result = state_ledger.build_verified_prose_context(state, 76, paths)
            self.assertIn('周予安在第75章完成动作。', result)
            self.assertNotIn('周予安在第74章完成动作。', result)
            self.assertNotIn('周予安在第76章完成动作。', result)
            self.assertIn(state['applied_chapters']['75'], result)

    def test_changed_formal_source_cannot_be_treated_as_verified(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / '第0075章.txt'
            path.write_text('被改过的正文', encoding='utf-8')
            with self.assertRaisesRegex(state_ledger.StateLedgerError, '哈希'):
                state_ledger.build_verified_prose_context(
                    {'applied_chapters': {'75': 'f' * 64}}, 76, {75: path})

    def test_prose_budget_preserves_complete_chapters_not_cut_quotes(self):
        with tempfile.TemporaryDirectory() as root:
            text = '第一句。' + '中间内容。' * 20 + '末尾事实。'
            path = Path(root) / '第0075章.txt'
            path.write_text(text, encoding='utf-8')
            result = state_ledger.build_verified_prose_context(
                {'applied_chapters': {'75': state_ledger.chapter_sha256(text)}},
                76, {75: path}, max_chars=40)
            self.assertNotIn('第一句', result)

    def test_audit_separates_historical_proof_from_current_claim_quote(self):
        system, prompt = state_ledger.build_delta_audit_prompts(
            {}, state_context='摘要' * 6000, chapter_text='本章原文',
            verified_source_context='第75章：本人回做使接应人减速。末尾原文证据')
        self.assertIn('末尾原文证据', prompt)
        self.assertIn('不能因为摘要未登记而判为未发生', system)
        self.assertIn('不能替代本章条目的 evidence_quote', system)
        self.assertIn('旁白知道不等于角色知道', system)
        self.assertIn('同一份旧资源的无依据恢复仍必须拒绝', system)


class StateLedgerTests(unittest.TestCase):
    def test_long_evidence_keeps_result_and_location_after_character_320(self):
        quote = '林舟沿着走廊寻找值班员。' + '他经过一扇紧闭的门。' * 35 + '林舟到达七号仓门前。'
        payload = {'chapter': 1, 'events': [{
            'event_type': 'world', 'summary': '林舟到达七号仓门前',
            'location': '七号仓门前', 'evidence_quote': quote,
        }]}
        delta, issues = state_ledger.validate_delta(payload, 1, quote)
        self.assertEqual([], issues)
        self.assertEqual(quote, delta['events'][0]['evidence_quote'])
        again, issues = state_ledger.validate_delta(delta, 1, quote)
        self.assertEqual([], issues)
        self.assertEqual(delta, again)

    def test_long_evidence_cannot_hide_fabricated_suffix_after_valid_prefix(self):
        quote = '林舟等在门外。' * 55
        payload = {'chapter': 1, 'events': [{
            'event_type': 'world', 'summary': '林舟等在门外',
            'evidence_quote': quote + '正文没有这个结论。',
        }]}
        _, issues = state_ledger.validate_delta(payload, 1, quote)
        self.assertTrue(any('evidence_quote 无法' in issue for issue in issues))

    def test_audit_reject_quote_checks_its_entire_suffix(self):
        quote = '林舟等在门外。' * 55
        delta = {'events': [{'summary': '林舟等待', 'evidence_quote': quote}]}
        for path in ('events[0]', 'draft'):
            with self.subTest(path=path):
                issues = state_ledger.validate_delta_audit({
                    'pass': False, 'reject': [{
                        'path': path, 'reason': '连续性错误',
                        'draft_quote': quote + '正文没有这个结论。',
                    }],
                }, delta, chapter_text=quote)
                self.assertTrue(any('没有可在正文定位' in issue for issue in issues))

    def test_audit_missing_core_resource_blocks_even_if_other_claims_pass(self):
        text = '林舟用完最后一次尝试，剩余次数归零。'
        delta = {'resources': [], 'events': [{'summary': '林舟离开'}]}
        for missing in ([{'category': 'resources', 'evidence_quote': text, 'reason': '未登记余额归零'}],
                        [{'category': 'resources', 'evidence_quote': '无此原句', 'reason': '归零'}], {}):
            with self.subTest(missing=missing):
                issues = state_ledger.validate_delta_audit(
                    {'pass': True, 'reject': [], 'missing': missing}, delta, text)
                self.assertTrue(issues)
        self.assertEqual([], state_ledger.validate_delta_audit(
            {'pass': True, 'reject': [], 'missing': []}, delta, text))

    def test_missing_character_location_preserves_actionable_repair_evidence(self):
        quote = '周予安站在康桥报名处，握着手机说话。'
        delta = {'characters': [], 'events': [{'summary': quote}]}
        for passed in (False, True):
            with self.subTest(passed=passed):
                issues = state_ledger.validate_delta_audit({
                    'pass': passed, 'reject': [], 'missing': [{
                        'category': 'characters', 'evidence_quote': quote,
                        'reason': '未登记周予安章末现实地点康桥报名处',
                    }],
                }, delta, quote)
                self.assertIn(
                    f'characters提取遗漏：未登记周予安章末现实地点康桥报名处；补提取原句：{quote}',
                    issues,
                )
                self.assertNotIn('独立证据审计遗漏项没有可在正文定位的完整证据', issues)

    def test_missing_character_location_still_requires_actual_full_quote(self):
        quote = '周予安站在康桥报名处。'
        for category, evidence, reason in (
            ('characters', '周予安已经回家。', '地点漏记'),
            ('characters', quote + '没有这个后缀。', '地点漏记'),
            ('characters', quote, ''),
            ('invented', quote, '地点漏记'),
        ):
            with self.subTest(category=category, evidence=evidence, reason=reason):
                issues = state_ledger.validate_delta_audit({
                    'pass': True, 'reject': [], 'missing': [{
                        'category': category, 'evidence_quote': evidence, 'reason': reason,
                    }],
                }, {}, quote)
                self.assertIn('独立证据审计遗漏项没有可在正文定位的完整证据', issues)

    def test_no_missing_report_does_not_invent_character_movement(self):
        quote = '周予安打算明天去康桥，回忆起昨天的训练。'
        self.assertEqual([], state_ledger.validate_delta_audit(
            {'pass': True, 'reject': [], 'missing': []}, {'characters': []}, quote))

    def test_resource_context_preserves_distinct_batches_and_source_chapters(self):
        state = state_ledger.initial_state()
        state['current_chapter'] = 8
        state['characters'] = {'林舟': {'last_seen_chapter': 8}}
        state['resources'] = {
            'old': {'owner': '林舟', 'item': '旧场次尝试', 'quantity': 0,
                    'status': '已经耗尽', 'last_changed_chapter': 3},
            'new': {'owner': '林舟', 'item': '本场尝试', 'quantity': 100,
                    'status': '新比赛独立额度', 'last_changed_chapter': 8},
        }
        before = json.dumps(state, ensure_ascii=False, sort_keys=True)
        context, _ = state_ledger.render_context(state, 9, '林舟选择片段')
        self.assertIn('旧场次尝试」：数量=0；已经耗尽；最后更新第3章', context)
        self.assertIn('本场尝试」：数量=100；新比赛独立额度；最后更新第8章', context)
        self.assertEqual(before, json.dumps(state, ensure_ascii=False, sort_keys=True))

    def test_resource_audit_requires_instance_evidence_without_ignoring_old_limits(self):
        system, _ = state_ledger.build_delta_audit_prompts({'resources': []})
        self.assertIn('先核对是否属于同一场次、批次或时段', system)
        self.assertIn('同一份旧资源的无依据恢复仍必须拒绝', system)
        self.assertIn('不得仅因章号较新就认定为新资源', system)

    def test_prune_overflow_items_deduplicates_without_discarding_unique_facts(self):
        events = [
            {"summary": f"事件{i}", "evidence_quote": f"证据{i}"}
            for i in range(10)
        ]
        payload = {
            "chapter": 1,
            "events": events + [dict(events[0]), {
                "summary": "溢出事件",
                "evidence_quote": "溢出证据",
            }],
        }

        sanitized, pruned = state_ledger.prune_overflow_items(payload)

        self.assertEqual(11, len(sanitized["events"]))
        self.assertEqual(events, sanitized["events"][:10])
        self.assertEqual("溢出事件", sanitized["events"][-1]["summary"])
        self.assertEqual(["events[10]:duplicate"], pruned)
        self.assertEqual(12, len(payload["events"]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plot = Path(self.temp.name) / "plot"
        self.plot.mkdir(parents=True)
        self.chapter = (
            "第1章 雨夜旧站\n\n"
            "林舟在旧站醒来，左臂的伤口仍在渗血。"
            "他从值班桌上拿起一把铜钥匙，清楚看见钥匙柄刻着七号。"
            "广播忽然响起：七号仓门将在午夜开启。"
        )

    def tearDown(self):
        self.temp.cleanup()

    def valid_delta(self):
        return {
            "chapter": 1,
            "characters": [
                {
                    "name": "林舟",
                    "location": "旧站",
                    "condition": "左臂伤口仍在渗血",
                    "emotion": "",
                    "status": "active",
                    "knowledge_add": ["钥匙柄刻着七号"],
                    "abilities_add": [],
                    "evidence_quote": "他从值班桌上拿起一把铜钥匙，清楚看见钥匙柄刻着七号。",
                }
            ],
            "resources": [
                {
                    "owner": "林舟",
                    "item": "铜钥匙",
                    "action": "GAIN",
                    "quantity_change": 1,
                    "quantity": None,
                    "status": "available",
                    "evidence_quote": "他从值班桌上拿起一把铜钥匙，清楚看见钥匙柄刻着七号。",
                }
            ],
            "relationships": [],
            "hooks": [
                {
                    "id": "",
                    "label": "七号仓门午夜开启",
                    "action": "OPEN",
                    "due_by": 3,
                    "evidence_quote": "广播忽然响起：七号仓门将在午夜开启。",
                }
            ],
            "subplots": [],
            "events": [
                {
                    "event_type": "investigation",
                    "summary": "林舟取得刻着七号的铜钥匙",
                    "evidence_quote": "他从值班桌上拿起一把铜钥匙，清楚看见钥匙柄刻着七号。",
                }
            ],
        }

    def commit_simple(self, chapter):
        text = f"第{chapter}章\n\n林舟完成了第{chapter}次记录。"
        delta = {
            "chapter": chapter,
            "characters": [],
            "resources": [],
            "relationships": [],
            "hooks": [],
            "subplots": [],
            "events": [{
                "event_type": "investigation",
                "summary": f"林舟完成第{chapter}次记录",
                "evidence_quote": f"林舟完成了第{chapter}次记录。",
            }],
        }
        return state_ledger.commit_validated_delta(self.plot, delta, text)

    def test_rejects_quote_and_entity_not_grounded_in_chapter(self):
        delta = self.valid_delta()
        delta["characters"].append(
            {
                "name": "不存在的人",
                "location": "火星",
                "condition": "健康",
                "emotion": "",
                "status": "active",
                "knowledge_add": ["幕后真凶是站长"],
                "abilities_add": ["瞬移"],
                "evidence_quote": "站长把全部真相告诉了他。",
            }
        )
        normalized, issues = state_ledger.validate_delta(
            delta, 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(normalized["chapter"], 1)
        self.assertTrue(any("evidence_quote" in issue for issue in issues))
        self.assertTrue(any("name 未在正文" in issue for issue in issues))

    def test_requires_at_least_one_event(self):
        delta = self.valid_delta()
        delta["events"] = []
        _normalized, issues = state_ledger.validate_delta(
            delta, 1, self.chapter, state_ledger.initial_state()
        )
        self.assertTrue(any("events 至少" in issue for issue in issues))

    def test_normalization_is_idempotent_when_long_quote_truncates_on_space(self):
        prefix = "林舟和值班员完成记录。"
        quote = prefix + ("甲" * (319 - len(prefix))) + " 后续证据仍在正文。"
        chapter = f"第1章\n\n{quote}"
        payload = {
            "chapter": 1,
            "characters": [],
            "resources": [],
            "relationships": [{
                "a": "林舟",
                "b": "值班员",
                "type": "交接",
                "status": "完成记录交接",
                "evidence_quote": quote,
            }],
            "hooks": [],
            "subplots": [],
            "events": [{
                "event_type": "investigation",
                "summary": "林舟和值班员完成记录",
                "evidence_quote": prefix,
            }],
        }

        normalized, issues = state_ledger.validate_delta(
            payload, 1, chapter, state_ledger.initial_state()
        )
        self.assertEqual([], issues)
        self.assertFalse(normalized["relationships"][0]["evidence_quote"].endswith(" "))
        second, second_issues = state_ledger.validate_delta(
            normalized, 1, chapter, state_ledger.initial_state()
        )
        self.assertEqual([], second_issues)
        self.assertEqual(normalized, second)
        state_ledger.commit_validated_delta(self.plot, normalized, chapter)

    def test_extraction_prompt_requires_event_subject_action_and_result_in_quote(self):
        _system, prompt = state_ledger.build_extraction_prompts(
            1, self.chapter, "第1章细纲", "暂无既有正史"
        )
        self.assertIn("每个关键主语、动作和结果", prompt)
        self.assertIn("只引用乙接球、加速或射门，不得反推甲已传球", prompt)

    def test_event_time_duration_and_location_are_grounded_in_same_quote(self):
        chapter = (
            "第1章 雨夜旧站\n\n"
            "午夜，林舟在旧站值班室守了三分钟，等到七号灯亮起。"
        )
        delta = self.valid_delta()
        delta["characters"] = []
        delta["resources"] = []
        delta["hooks"] = []
        delta["events"] = [{
            "event_type": "investigation",
            "summary": "林舟等到七号灯亮起",
            "time_anchor": "午夜",
            "duration": "三分钟",
            "location": "旧站值班室",
            "evidence_quote": "午夜，林舟在旧站值班室守了三分钟，等到七号灯亮起。",
        }]
        normalized, issues = state_ledger.validate_delta(
            delta, 1, chapter, state_ledger.initial_state()
        )
        self.assertEqual([], issues)
        state = state_ledger.commit_validated_delta(self.plot, normalized, chapter)
        event = state["timeline"][0]
        self.assertEqual("午夜", event["time_anchor"])
        self.assertEqual("三分钟", event["duration"])
        self.assertEqual("旧站值班室", event["location"])
        self.assertEqual(1, event["event_order"])
        self.assertEqual(event, state["event_history"][0])

        delta["events"][0]["time_anchor"] = "第二天"
        normalized_without_time, time_issues = state_ledger.validate_delta(
            delta, 1, chapter, state_ledger.initial_state()
        )
        self.assertEqual([], time_issues)
        self.assertEqual("", normalized_without_time["events"][0]["time_anchor"])
        delta["events"][0]["time_anchor"] = "午夜"

        delta["events"][0]["location"] = "火星基地"
        _normalized, issues = state_ledger.validate_delta(
            delta, 1, chapter, state_ledger.initial_state()
        )
        self.assertTrue(any("location" in issue for issue in issues))

    def test_prune_unlocatable_evidence_items_is_narrow(self):
        delta = self.valid_delta()
        delta["events"].append({
            "event_type": "reveal",
            "summary": "模型补写的无证事实",
            "evidence_quote": "正文里根本没有这句话",
        })
        _normalized, issues = state_ledger.validate_delta(
            delta, 1, self.chapter, state_ledger.initial_state()
        )
        sanitized, removed = state_ledger.prune_unlocatable_evidence_items(
            delta, issues
        )
        self.assertEqual(removed, ["events[1]"])
        normalized, retry_issues = state_ledger.validate_delta(
            sanitized, 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(retry_issues, [])
        self.assertEqual(len(normalized["events"]), 1)

        unchanged, removed = state_ledger.prune_unlocatable_evidence_items(
            delta,
            issues + ["events 至少要登记一条本章已发生的核心事件，不能用空增量跳过正史记忆"],
        )
        self.assertIs(unchanged, delta)
        self.assertEqual(removed, [])

    def test_prune_audit_rejected_items_keeps_grounded_remainder(self):
        delta = {
            "chapter": 31,
            "characters": [],
            "resources": [{"item": "overclaim"}],
            "relationships": [],
            "hooks": [],
            "subplots": [],
            "events": [
                {"summary": "overclaim"},
                {"summary": "grounded"},
            ],
        }
        audit = {
            "pass": False,
            "reject": [
                {"path": "resources[0]"},
                {"path": "events[0]"},
            ],
        }
        sanitized, removed = state_ledger.prune_audit_rejected_items(delta, audit)
        self.assertEqual(removed, ["events[0]", "resources[0]"])
        self.assertEqual(sanitized["resources"], [])
        self.assertEqual(sanitized["events"], [{"summary": "grounded"}])
        self.assertEqual(len(delta["events"]), 2)

    def test_prune_audit_rejected_items_never_masks_draft_or_all_events(self):
        delta = {
            "chapter": 31,
            "characters": [],
            "resources": [],
            "relationships": [],
            "hooks": [],
            "subplots": [],
            "events": [{"summary": "only"}],
        }
        unchanged, removed = state_ledger.prune_audit_rejected_items(
            delta, {"reject": [{"path": "draft"}]}
        )
        self.assertIs(unchanged, delta)
        self.assertEqual(removed, [])

        unchanged, removed = state_ledger.prune_audit_rejected_items(
            delta, {"reject": [{"path": "events[0]"}]}
        )
        self.assertIs(unchanged, delta)
        self.assertEqual(removed, [])

    def test_commit_is_idempotent_and_rebuilds_from_immutable_delta(self):
        delta, issues = state_ledger.validate_delta(
            self.valid_delta(), 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(issues, [])
        state = state_ledger.commit_validated_delta(self.plot, delta, self.chapter)
        again = state_ledger.commit_validated_delta(self.plot, delta, self.chapter)
        self.assertEqual(state["current_chapter"], 1)
        self.assertEqual(state, again)
        self.assertIn("钥匙柄刻着七号", state["characters"]["林舟"]["knowledge"])
        self.assertEqual(len(state["event_history"]), 1)

        (self.plot / "story_state.json").write_text("{broken", encoding="utf-8")
        rebuilt = state_ledger.load_state(self.plot)
        self.assertEqual(rebuilt["current_chapter"], 1)
        self.assertEqual(rebuilt["characters"]["林舟"]["location"], "旧站")
        self.assertEqual(len(rebuilt["event_history"]), 1)

        state_path = self.plot / "story_state.json"
        tampered = json.loads(state_path.read_text(encoding="utf-8"))
        tampered["characters"]["林舟"]["location"] = "不存在的火星基地"
        state_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        integrity_rebuilt = state_ledger.load_state(self.plot)
        self.assertEqual(integrity_rebuilt["characters"]["林舟"]["location"], "旧站")

    def test_explicit_unknown_location_clears_stale_place(self):
        prior = state_ledger.initial_state()
        prior["characters"]["林舟"] = {
            "location": "旧站停车处", "condition": "", "emotion": "",
            "status": "active", "knowledge": [], "abilities": [],
            "last_seen_chapter": 1, "evidence": [],
        }
        text = "第2章 离开\n\n林舟背起包，跟着队友走了出去。"
        delta = {
            "chapter": 2,
            "characters": [{
                "name": "林舟", "location": "", "location_state": "UNKNOWN",
                "condition": "", "emotion": "", "status": "",
                "knowledge_add": [], "abilities_add": [],
                "evidence_quote": "林舟背起包，跟着队友走了出去。",
            }],
            "resources": [], "relationships": [], "hooks": [], "subplots": [],
            "events": [{
                "event_type": "world", "summary": "林舟离开原处",
                "evidence_quote": "林舟背起包，跟着队友走了出去。",
            }],
        }
        normalized, issues = state_ledger.validate_delta(delta, 2, text, prior)
        self.assertEqual([], issues)
        self.assertEqual("UNKNOWN", normalized["characters"][0]["location_state"])
        applied = state_ledger._apply_delta_to_state(prior, normalized, "chapter-two")
        self.assertEqual("", applied["characters"]["林舟"]["location"])

    def test_no_change_location_preserves_prior_and_legacy_rows_infer_mode(self):
        prior = state_ledger.initial_state()
        prior["characters"]["林舟"] = {
            "location": "旧站", "condition": "", "emotion": "",
            "status": "active", "knowledge": [], "abilities": [],
            "last_seen_chapter": 0, "evidence": [],
        }
        delta = self.valid_delta()
        delta["characters"][0]["location"] = ""
        normalized, issues = state_ledger.validate_delta(
            delta, 1, self.chapter, prior
        )
        self.assertEqual([], issues)
        self.assertEqual("NO_CHANGE", normalized["characters"][0]["location_state"])
        applied = state_ledger._apply_delta_to_state(prior, normalized, "legacy-row")
        self.assertEqual("旧站", applied["characters"]["林舟"]["location"])

    def test_location_state_rejects_contradictory_payload(self):
        delta = self.valid_delta()
        delta["characters"][0]["location_state"] = "UNKNOWN"
        _, issues = state_ledger.validate_delta(
            delta, 1, self.chapter, state_ledger.initial_state()
        )
        self.assertTrue(any("location_state=UNKNOWN" in issue for issue in issues))

    def test_refuses_same_chapter_with_changed_text(self):
        delta, issues = state_ledger.validate_delta(
            self.valid_delta(), 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(issues, [])
        state_ledger.commit_validated_delta(self.plot, delta, self.chapter)
        with self.assertRaises(state_ledger.StateLedgerError):
            state_ledger.commit_validated_delta(
                self.plot, delta, self.chapter + "后来这一段被改写。"
            )

    def test_resource_explicit_balance_survives_sparse_use_events_and_replay(self):
        for chapter, action, change, balance in [(1, "SET", None, 55), (2, "USE", -3, 52), (3, "USE", -9, 0)]:
            text = f"第{chapter}章\n\n林舟的尝试次数还剩{balance}次。"
            delta = {"chapter": chapter, "characters": [], "relationships": [],
                     "hooks": [], "subplots": [], "events": [{
                         "event_type": "resource", "summary": f"林舟的尝试次数还剩{balance}次",
                         "evidence_quote": f"林舟的尝试次数还剩{balance}次。"}], "resources": [{
                         "owner": "林舟", "item": "尝试次数", "action": action,
                         "quantity_change": change, "quantity": balance, "status": "",
                         "evidence_quote": f"林舟的尝试次数还剩{balance}次。"}]}
            delta, issues = state_ledger.validate_delta(
                delta, chapter, text, state_ledger.load_state(self.plot)
            )
            self.assertEqual([], issues)
            state = state_ledger.commit_validated_delta(self.plot, delta, text)
            self.assertEqual(balance, next(iter(state["resources"].values()))["quantity"])
        before = {p.name: p.read_bytes() for p in (self.plot / "state_deltas").glob("chapter_*.json")}
        rebuilt = state_ledger.rebuild_state_from_deltas(self.plot)
        self.assertEqual(0, next(iter(rebuilt["resources"].values()))["quantity"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.plot / "state_deltas").glob("chapter_*.json")})

    def test_resource_gain_can_establish_explicit_balance_without_prior_quantity(self):
        delta = self.valid_delta()
        delta["resources"][0]["quantity"] = 1
        delta, issues = state_ledger.validate_delta(
            delta, 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual([], issues)
        state = state_ledger.commit_validated_delta(self.plot, delta, self.chapter)
        self.assertEqual(1, next(iter(state["resources"].values()))["quantity"])

    def test_resource_change_only_still_accumulates_and_refuses_overdraft(self):
        state = state_ledger.initial_state()
        for chapter, action, change, balance, expected in [
            (1, "SET", None, 10, 10), (2, "USE", -3, None, 7),
            (3, "GAIN", 2, None, 9), (4, "LOSE", -9, None, 0),
        ]:
            delta = {"chapter": chapter, "resources": [{
                "owner": "林舟", "item": "次数", "action": action,
                "quantity": balance, "quantity_change": change,
                "status": "", "evidence_quote": "测试资源变化",
            }]}
            state = state_ledger._apply_delta_to_state(state, delta, "test-hash")
            self.assertEqual(expected, next(iter(state["resources"].values()))["quantity"])
        delta["chapter"] = 5
        delta["resources"][0].update(action="USE", quantity_change=-1)
        with self.assertRaises(state_ledger.StateLedgerError):
            state_ledger._apply_delta_to_state(state, delta, "test-hash")

    def test_resource_negative_balance_rejected_for_use_not_only_set(self):
        delta = self.valid_delta()
        delta["resources"][0].update(action="USE", quantity=-1)
        _, issues = state_ledger.validate_delta(delta, 1, self.chapter, state_ledger.initial_state())
        self.assertTrue(any("quantity 不能为负数" in issue for issue in issues))

    def test_backfill_legacy_character_knowledge_reseals_forward_chain(self):
        delta, issues = state_ledger.validate_delta(
            self.valid_delta(), 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(issues, [])
        state_ledger.commit_validated_delta(self.plot, delta, self.chapter)

        chapter_two = "第2章 仓门前\n\n林舟走到七号仓门前，门仍然关闭。"
        delta_two = {
            "chapter": 2,
            "characters": [{
                "name": "林舟", "location": "七号仓门前", "condition": "",
                "emotion": "", "status": "active", "knowledge_add": [],
                "abilities_add": [], "evidence_quote": "林舟走到七号仓门前，门仍然关闭。",
            }],
            "resources": [], "relationships": [], "hooks": [], "subplots": [],
            "events": [{
                "event_type": "world", "summary": "林舟抵达七号仓门前",
                "evidence_quote": "林舟走到七号仓门前，门仍然关闭。",
            }],
        }
        normalized_two, issues_two = state_ledger.validate_delta(
            delta_two, 2, chapter_two, state_ledger.load_state(self.plot)
        )
        self.assertEqual(issues_two, [])
        state_ledger.commit_validated_delta(self.plot, normalized_two, chapter_two)

        evidence = (
            "林舟在旧站醒来，左臂的伤口仍在渗血。"
            "他从值班桌上拿起一把铜钥匙，清楚看见钥匙柄刻着七号。"
            "广播忽然响起：七号仓门将在午夜开启。"
        )
        repaired = state_ledger.backfill_legacy_character_knowledge(
            self.plot, 1, "林舟", "七号仓门将在午夜开启", evidence, self.chapter
        )
        self.assertEqual(repaired["current_chapter"], 2)
        self.assertIn(
            "七号仓门将在午夜开启", repaired["characters"]["林舟"]["knowledge"]
        )
        self.assertEqual(
            state_ledger.rebuild_state_from_deltas(self.plot)["current_chapter"], 2
        )
        repeated = state_ledger.backfill_legacy_character_knowledge(
            self.plot, 1, "林舟", "七号仓门将在午夜开启", evidence, self.chapter
        )
        self.assertEqual(repeated["current_chapter"], 2)

    def test_last_chapter_can_be_explicitly_superseded_without_rewriting_history(self):
        delta, issues = state_ledger.validate_delta(
            self.valid_delta(), 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(issues, [])
        state_ledger.commit_validated_delta(self.plot, delta, self.chapter)

        replacement_text = "第1章 雨夜旧站\n\n林舟在旧站醒来。他拿起一枚铁徽章。"
        replacement = {
            "chapter": 1,
            "characters": [{
                "name": "林舟", "location": "旧站", "condition": "", "emotion": "",
                "status": "active", "knowledge_add": [], "abilities_add": [],
                "evidence_quote": "林舟在旧站醒来。",
            }],
            "resources": [{
                "owner": "林舟", "item": "铁徽章", "action": "GAIN",
                "quantity_change": 1, "quantity": None, "status": "available",
                "evidence_quote": "他拿起一枚铁徽章。",
            }],
            "relationships": [], "hooks": [], "subplots": [],
            "events": [{
                "event_type": "resource", "summary": "林舟取得铁徽章",
                "evidence_quote": "他拿起一枚铁徽章。",
            }],
        }
        prior = state_ledger.build_state_before_chapter(self.plot, 1)
        normalized, replacement_issues = state_ledger.validate_delta(
            replacement, 1, replacement_text, prior
        )
        self.assertEqual(replacement_issues, [])
        archive = state_ledger.supersede_last_chapter_delta(self.plot, 1)
        self.assertTrue(Path(archive).exists())
        updated = state_ledger.commit_validated_delta(
            self.plot, normalized, replacement_text
        )
        resources = list(updated["resources"].values())
        self.assertEqual([item["item"] for item in resources], ["铁徽章"])
        self.assertEqual(updated["current_chapter"], 1)

    def test_context_enforces_knowledge_boundary_cooldown_and_stale_subplot(self):
        state = state_ledger.initial_state()
        state["current_chapter"] = 10
        state["characters"]["林舟"] = {
            "location": "旧站",
            "condition": "左臂受伤",
            "emotion": "",
            "status": "active",
            "knowledge": ["钥匙柄刻着七号"],
            "abilities": [],
            "last_seen_chapter": 10,
            "evidence": [],
        }
        state["subplots"]["subplot_old"] = {
            "id": "subplot_old",
            "label": "失踪的值班员",
            "status": "OPEN",
            "opened_chapter": 1,
            "last_advanced_chapter": 2,
        }
        state["event_history"].append(
            {"chapter": 10, "event_type": "conflict", "summary": "林舟刚经历追逐战"}
        )
        context, manifest = state_ledger.render_context(
            state, 11, "林舟寻找值班员", "", stale_warn_chapters=8
        )
        self.assertIn("摘要不穷尽历史事实", context)
        self.assertIn("仅旁白披露不能充当角色知识", context)
        self.assertIn("角色不得知道其“已知”列表之外", context)
        self.assertIn("事件冷却", context)
        self.assertIn("非正史硬约束，不得据此拒绝正文", context)
        self.assertIn("若本章细纲仍要求同类事件，以细纲为准", context)
        self.assertIn("失踪的值班员", context)
        self.assertEqual(manifest["stale_subplots"][0]["id"], "subplot_old")

    def test_context_trace_contains_source_hashes(self):
        source = self.plot / "story_bible.json"
        source.write_text(json.dumps({"title": "测试"}, ensure_ascii=False), encoding="utf-8")
        manifest = state_ledger.compile_chapter_context(
            self.plot,
            1,
            "林舟进入旧站",
            "",
            source_paths={"story_bible": source},
        )
        trace = Path(manifest["trace_path"])
        self.assertTrue(trace.exists())
        loaded = json.loads(trace.read_text(encoding="utf-8"))
        self.assertEqual(len(loaded["source_hashes"]["story_bible"]["sha256"]), 64)
        self.assertEqual(loaded["outline_sha256"], manifest["outline_sha256"])

    def test_deleted_last_delta_is_not_silently_accepted(self):
        self.commit_simple(1)
        self.commit_simple(2)
        (self.plot / "state_deltas" / "chapter_0002.json").unlink()
        with self.assertRaisesRegex(state_ledger.StateLedgerError, "被删除"):
            state_ledger.load_state(self.plot)

    def test_missing_middle_delta_breaks_continuous_chain(self):
        self.commit_simple(1)
        self.commit_simple(2)
        self.commit_simple(3)
        (self.plot / "state_deltas" / "chapter_0002.json").unlink()
        audit = state_ledger.audit_state(self.plot, 3)
        self.assertEqual("FAIL", audit["status"])
        self.assertIn("断号", audit["message"])

    def test_tampered_v2_delta_fails_content_hash(self):
        self.commit_simple(1)
        path = self.plot / "state_deltas" / "chapter_0001.json"
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["delta"]["events"][0]["summary"] = "被人篡改的事件"
        path.write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(state_ledger.StateLedgerError, "内容哈希不一致"):
            state_ledger.load_state(self.plot)

    def test_legacy_v1_delta_can_still_be_rebuilt(self):
        self.commit_simple(1)
        path = self.plot / "state_deltas" / "chapter_0001.json"
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["schema_version"] = 1
        wrapper.pop("previous_delta_sha256", None)
        wrapper.pop("delta_sha256", None)
        path.write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")
        (self.plot / "story_state.json").write_text("{broken", encoding="utf-8")
        rebuilt = state_ledger.load_state(self.plot)
        self.assertEqual(1, rebuilt["current_chapter"])

    def test_independent_audit_rejects_claim_path(self):
        delta, issues = state_ledger.validate_delta(
            self.valid_delta(), 1, self.chapter, state_ledger.initial_state()
        )
        self.assertEqual(issues, [])
        audit_issues = state_ledger.validate_delta_audit(
            {
                "pass": False,
                "reject": [
                    {"path": "characters[0]", "reason": "证据只说明看见刻字，未支持其他字段"}
                ],
            },
            delta,
        )
        self.assertEqual(len(audit_issues), 1)
        self.assertIn("characters[0]", audit_issues[0])


def test_unique_overflow_is_preserved_and_remains_a_validation_error():
    for key, limit in state_ledger.MAX_ITEMS.items():
        rows = [{"evidence_quote": "林舟读到新通知。", "summary": str(i)}
                for i in range(limit + 1)]
        original = {"chapter": 1, key: rows}
        sanitized, removed = state_ledger.prune_overflow_items(original)
        assert sanitized[key] == rows
        assert removed == []
        _, issues = state_ledger.validate_delta(sanitized, 1, "林舟读到新通知。")
        assert any(f"{key} 条目过多" in issue for issue in issues)


def test_duplicate_state_rows_are_removed_even_below_capacity():
    row = {"summary": "林舟读到通知", "evidence_quote": "林舟读到通知。"}
    original = {"events": [row, dict(row)]}
    sanitized, removed = state_ledger.prune_overflow_items(original)
    assert sanitized["events"] == [row]
    assert removed == ["events[1]:duplicate"]
    assert len(original["events"]) == 2


def test_character_capacity_accepts_separate_evidence_without_losing_final_rows():
    rows = [{"name": "林舟", "knowledge_add": [f"通知编号{i}"],
             "evidence_quote": f"林舟读到通知编号{i}。"} for i in range(19)]
    text = "\n".join(row["evidence_quote"] for row in rows)
    payload = {"chapter": 1, "characters": rows, "events": [{
        "event_type": "reveal", "summary": "林舟读到通知编号0",
        "evidence_quote": rows[0]["evidence_quote"],
    }]}
    sanitized, removed = state_ledger.prune_overflow_items(payload)
    assert sanitized["characters"] == rows
    delta, issues = state_ledger.validate_delta(sanitized, 1, text)
    assert issues == []
    assert len(delta["characters"]) == 19
    assert removed == []


def test_extraction_prompt_exposes_bounded_state_capacity_without_truncation():
    _, prompt = state_ledger.build_extraction_prompts(1, "林舟读到通知。")
    for key, limit in state_ledger.MAX_ITEMS.items():
        assert f'"{key}": {limit}' in prompt
    assert "不得按顺序裁掉独有事实" in prompt
    assert "不能靠删掉有证据的关键变化来凑容量" in prompt


def test_context_keeps_recent_open_items_instead_of_only_oldest_items():
    state = state_ledger.initial_state()
    for key, limit in (("hooks", 10), ("subplots", 8)):
        for chapter in range(1, limit + 3):
            item_id = f"{key}_{chapter}"
            state[key][item_id] = {
                "id": item_id, "label": f"未结事项{chapter}", "status": "OPEN",
                "opened_chapter": chapter, "last_advanced_chapter": chapter,
                "due_by": None,
            }
    context, manifest = state_ledger.render_context(state, 20, max_chars=10000)
    for key, limit, manifest_key in (("hooks", 10, "active_hook_ids"), ("subplots", 8, "active_subplot_ids")):
        assert f"{key}_{limit + 2}" in manifest[manifest_key]
        assert f"{key}_1" not in manifest[manifest_key]
        assert len(manifest[manifest_key]) == limit
        assert f"{key}_{limit + 2}" in context


def test_context_keeps_explicit_old_and_due_items_among_recent_items():
    state = state_ledger.initial_state()
    for chapter in range(1, 15):
        state["hooks"][f"hook_{chapter}"] = {
            "id": f"hook_{chapter}", "label": f"旧事项{chapter}", "status": "OPEN",
            "opened_chapter": chapter, "last_advanced_chapter": chapter,
            "due_by": None,
        }
    state["hooks"]["hook_1"]["label"] = "车站遗留的红色钥匙"
    state["hooks"]["hook_2"]["due_by"] = 15
    _, manifest = state_ledger.render_context(state, 16, "找回车站遗留的红色钥匙", max_chars=10000)
    assert manifest["active_hook_ids"][:2] == ["hook_1", "hook_2"]
    assert "hook_14" in manifest["active_hook_ids"]


def test_context_tracking_manifest_excludes_rows_removed_by_budget():
    state = state_ledger.initial_state()
    for key in ("hooks", "subplots"):
        state[key]["current"] = {
            "id": "current", "label": "仍待处理的事项", "status": "OPEN",
            "opened_chapter": 1, "last_advanced_chapter": 1,
        }
    context, manifest = state_ledger.render_context(state, 2, max_chars=150)
    assert "上下文已按预算截断" in context
    assert manifest["active_hook_ids"] == []
    assert manifest["active_subplot_ids"] == []


def test_context_tracking_manifest_excludes_partially_visible_row():
    state = state_ledger.initial_state()
    state["hooks"]["hook_partial"] = {
        "id": "hook_partial", "label": "该条余下的长说明" * 12, "status": "OPEN",
        "opened_chapter": 1, "last_advanced_chapter": 1,
    }
    complete, _ = state_ledger.render_context(state, 2, max_chars=10000)
    budget = complete.index("- 伏笔ID=hook_partial") + 65
    context, manifest = state_ledger.render_context(state, 2, max_chars=budget)
    assert "hook_partial" in context
    assert "上下文已按预算截断" in context
    assert manifest["active_hook_ids"] == []


if __name__ == "__main__":
    unittest.main()
