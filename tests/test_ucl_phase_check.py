import copy
import json
from pathlib import Path
import tempfile
import unittest

import ucl_phase_check as check


class PhaseCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=json.loads((Path(__file__).parent/'fixtures'/'ucl_phase_36_valid.json').read_text(encoding='utf-8'))

    def data(self):
        return copy.deepcopy(self.fixture)

    def assert_invalid(self,data,code):
        report=check.check_data(data)
        self.assertFalse(report['structure_valid'])
        self.assertEqual(report['standings'],[])
        self.assertIn(code,[x['code'] for x in report['issues']])

    def test_complete_structure_and_scores(self):
        r=check.check_data(self.data())
        self.assertTrue(r['structure_valid']);self.assertTrue(r['full_tiebreak_resolution'])
        self.assertEqual(r['match_count'],144)
        self.assertEqual(r['goals_for_total'],r['goals_against_total'])
        self.assertFalse(r['confirmed_results']);self.assertFalse(r['formal_adoption'])

    def test_no_partial_standings_for_missing_match(self):
        d=self.data();d['matches'].pop();self.assert_invalid(d,'EXPECTED_144_MATCHES')

    def test_duplicate_team(self):
        d=self.data();d['teams'][1]['name']=d['teams'][0]['name']
        self.assert_invalid(d,'INVALID_OR_DUPLICATE_TEAM')

    def test_bool_is_not_pot(self):
        d=self.data();d['teams'][0]['pot']=True
        self.assert_invalid(d,'INVALID_OR_DUPLICATE_TEAM')

    def test_bool_is_not_score(self):
        d=self.data();d['matches'][0]['score']=[True,0]
        self.assert_invalid(d,'INVALID_MATCH')

    def test_unknown_team(self):
        d=self.data();d['matches'][0]['away']='不存在的队'
        self.assert_invalid(d,'INVALID_MATCH')

    def test_unhashable_team_is_rejected(self):
        d=self.data();d['matches'][0]['away']=['bad']
        self.assert_invalid(d,'INVALID_MATCH')

    def test_repeated_pair(self):
        d=self.data();a,b=d['matches'][0],d['matches'][18]
        b['home'],b['away']=a['away'],a['home']
        self.assert_invalid(d,'REPEATED_OPPONENT_PAIR')

    def test_same_association(self):
        d=self.data();m=d['matches'][0];teams={t['name']:t for t in d['teams']}
        teams[m['away']]['association']=teams[m['home']]['association']
        self.assert_invalid(d,'SAME_ASSOCIATION_MATCH')

    def test_home_pot_and_total_requirements(self):
        d=self.data();m=d['matches'][0];m['home'],m['away']=m['away'],m['home']
        self.assert_invalid(d,'EXPECTED_ONE_HOME_ONE_AWAY_PER_POT')

    def test_date_consistency(self):
        d=self.data();d['matches'][0]['date']='2030-09-17'
        self.assert_invalid(d,'INCONSISTENT_ROUND_DATE')

    def test_unresolved_ties_not_alphabetic_rankings(self):
        d=self.data()
        for m in d['matches']:m['score']=[0,0]
        r=check.check_data(d);self.assertTrue(r['structure_valid'])
        self.assertFalse(r['full_tiebreak_resolution'])
        self.assertEqual(len(r['ties_after_score_criteria'][0]),36)
        self.assertTrue(all(s['rank'] is None and s['rank_min']==1 and s['rank_max']==36 for s in r['standings']))

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory(prefix='ucl_phase_test_') as temp:
            path=Path(temp)/'input.json';path.write_text('{"teams":[],"teams":[]}',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'duplicate JSON key'):check.check_file(path)

    def test_bounded_file_read(self):
        with tempfile.TemporaryDirectory(prefix='ucl_phase_test_') as temp:
            path=Path(temp)/'input.json';path.write_bytes(b' '*(check.MAX_BYTES+1))
            with self.assertRaisesRegex(ValueError,'bounded'):check.check_file(path)


if __name__=='__main__':unittest.main()
