"""Bounded, model-free check of a complete authored UEFA-style league phase."""
import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re

MAX_BYTES = 1024 * 1024


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def check_data(data):
    report = {'scope':'authored_complete_phase_only', 'read_only':True,
              'confirmed_results':False, 'formal_adoption':False,
              'structure_valid':False, 'full_tiebreak_resolution':False,
              'issues':[], 'standings':[], 'ties_after_score_criteria':[]}
    def issue(code, **details):
        if len(report['issues']) < 100:
            report['issues'].append({'code':code, **details})

    if (not isinstance(data,dict) or type(data.get('schema_version')) is not int
            or data['schema_version'] != 1 or data.get('scope') != 'authored_future_plan'):
        issue('INVALID_SCOPE_OR_SCHEMA'); return report
    teams, matches = data.get('teams'), data.get('matches')
    if not isinstance(teams,list) or len(teams) != 36:
        issue('EXPECTED_36_TEAMS'); return report
    if not isinstance(matches,list) or len(matches) != 144:
        issue('EXPECTED_144_MATCHES'); return report
    by_name = {}
    for row in teams:
        if (not isinstance(row,dict) or not isinstance(row.get('name'),str)
                or not 1 <= len(row['name']) <= 80 or row['name'].strip() != row['name']
                or row['name'] in by_name or not isinstance(row.get('association'),str)
                or not re.fullmatch(r'[A-Z]{3}',row['association'])
                or not integer(row.get('pot'),1,4)):
            issue('INVALID_OR_DUPLICATE_TEAM'); continue
        by_name[row['name']] = row
    if report['issues']:
        return report
    if Counter(row['pot'] for row in teams) != Counter({1:9,2:9,3:9,4:9}):
        issue('INVALID_POT_SIZES'); return report
    stats = {name:{'team':name,'points':0,'gf':0,'ga':0,'away_goals':0,
                   'wins':0,'away_wins':0} for name in by_name}
    opponents = defaultdict(list)
    venue = defaultdict(dict)
    seen_pairs = set()
    rounds = defaultdict(list)
    round_dates = defaultdict(set)
    for index,row in enumerate(matches):
        if (not isinstance(row,dict) or not integer(row.get('round'),1,8)
                or not isinstance(row.get('home'),str) or not isinstance(row.get('away'),str)
                or row.get('home') not in by_name or row.get('away') not in by_name
                or row.get('home') == row.get('away') or not isinstance(row.get('date'),str)
                or not isinstance(row.get('score'),list) or len(row['score']) != 2
                or not all(integer(v,0,99) for v in row['score'])):
            issue('INVALID_MATCH',index=index); continue
        try:
            played_on = date.fromisoformat(row['date'])
            if played_on.isoformat() != row['date']:
                raise ValueError('non-canonical date')
        except ValueError:
            issue('INVALID_DATE',index=index); continue
        home,away,day = row['home'],row['away'],row['round']
        pair = frozenset((home,away))
        if pair in seen_pairs:
            issue('REPEATED_OPPONENT_PAIR',index=index)
        seen_pairs.add(pair)
        if by_name[home]['association'] == by_name[away]['association']:
            issue('SAME_ASSOCIATION_MATCH',index=index)
        rounds[day].extend((home,away));round_dates[day].add(played_on)
        for name,other,side in ((home,away,'H'),(away,home,'A')):
            if day in venue[name]:
                issue('MULTIPLE_MATCHES_IN_ROUND',team=name,round=day)
            venue[name][day] = side
            opponents[name].append(other)
        gh,ga = row['score']
        for name,gf,against,is_away in ((home,gh,ga,False),(away,ga,gh,True)):
            s=stats[name];s['gf']+=gf;s['ga']+=against
            if is_away:s['away_goals']+=gf
            if gf>against:
                s['points']+=3;s['wins']+=1;s['away_wins']+=int(is_away)
            elif gf==against:s['points']+=1
    if report['issues']:
        return report
    for day in range(1,9):
        if Counter(rounds[day]) != Counter({name:1 for name in by_name}):
            issue('INCOMPLETE_ROUND',round=day)
        if len(round_dates[day]) != 1:
            issue('INCONSISTENT_ROUND_DATE',round=day)
    if all(len(round_dates[d])==1 for d in range(1,9)):
        dates=[next(iter(round_dates[d])) for d in range(1,9)]
        if any(a>=b for a,b in zip(dates,dates[1:])):
            issue('NON_INCREASING_ROUND_DATES')
    for name in by_name:
        opp=opponents[name]
        if len(opp)!=8 or len(set(opp))!=8:
            issue('EXPECTED_EIGHT_DIFFERENT_OPPONENTS',team=name)
        if Counter(by_name[o]['pot'] for o in opp)!=Counter({1:2,2:2,3:2,4:2}):
            issue('INVALID_OPPONENT_POT_COUNTS',team=name)
        if max(Counter(by_name[o]['association'] for o in opp).values(),default=0)>2:
            issue('TOO_MANY_OPPONENTS_FROM_ASSOCIATION',team=name)
        if set(venue[name])!=set(range(1,9)):
            issue('INCOMPLETE_TEAM_ROUNDS',team=name);continue
        sides=[venue[name][d] for d in range(1,9)]
        if Counter(sides)!=Counter({'H':4,'A':4}):
            issue('EXPECTED_FOUR_HOME_FOUR_AWAY',team=name)
        if sides[0]==sides[1] or sides[-1]==sides[-2]:
            issue('INVALID_FIRST_OR_LAST_PAIR_VENUES',team=name)
        if any(len(set(sides[i:i+3]))==1 for i in range(6)):
            issue('THREE_CONSECUTIVE_EQUAL_VENUES',team=name)
        home_pots=Counter();away_pots=Counter()
        for row in matches:
            if not isinstance(row,dict):continue
            if row.get('home')==name and row.get('away') in by_name:
                home_pots[by_name[row['away']]['pot']]+=1
            if row.get('away')==name and row.get('home') in by_name:
                away_pots[by_name[row['home']]['pot']]+=1
        if home_pots!=Counter({1:1,2:1,3:1,4:1}) or away_pots!=home_pots:
            issue('EXPECTED_ONE_HOME_ONE_AWAY_PER_POT',team=name)
    if report['issues']:
        return report
    # Article 18's score-derived criteria a-h. Do not invent cards/coefficient i-j.
    groups=defaultdict(list)
    for name,s in stats.items():
        s['gd']=s['gf']-s['ga']
        s['opponent_points']=sum(stats[o]['points'] for o in opponents[name])
        s['opponent_gd']=sum(stats[o]['gf']-stats[o]['ga'] for o in opponents[name])
        s['opponent_goals']=sum(stats[o]['gf'] for o in opponents[name])
        key=tuple(s[k] for k in ('points','gd','gf','away_goals','wins','away_wins',
                                 'opponent_points','opponent_gd','opponent_goals'))
        groups[key].append(s)
    position=1
    for key in sorted(groups,reverse=True):
        members=groups[key];last=position+len(members)-1
        if len(members)>1:
            report['ties_after_score_criteria'].append([s['team'] for s in members])
        for s in members:
            report['standings'].append(dict(s,rank_min=position,rank_max=last,
                                            rank=position if position==last else None))
        position=last+1
    report.update(structure_valid=True,
                  full_tiebreak_resolution=not report['ties_after_score_criteria'],
                  team_count=36,match_count=144,
                  total_points=sum(s['points'] for s in stats.values()),
                  goals_for_total=sum(s['gf'] for s in stats.values()),
                  goals_against_total=sum(s['ga'] for s in stats.values()))
    return report


def unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON key: '+key)
        result[key]=value
    return result


def reject_constant(value):
    raise ValueError('non-finite JSON number: '+value)


def check_file(path):
    with Path(path).open('rb') as stream:
        raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('source exceeds bounded 1 MiB read')
    data=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique_pairs,
                    parse_constant=reject_constant)
    report=check_data(data);report['source_sha256']=hashlib.sha256(raw).hexdigest()
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path');args=parser.parse_args(argv)
    try:report=check_file(args.path)
    except (OSError,UnicodeError,ValueError,TypeError) as exc:
        print(json.dumps({'structure_valid':False,'read_only':True,
                          'issues':[{'code':'SOURCE_READ_ERROR','message':str(exc)}]},ensure_ascii=False))
        return 2
    print(json.dumps(report,ensure_ascii=False));return 0 if report['structure_valid'] else 1


if __name__=='__main__':
    raise SystemExit(main())
