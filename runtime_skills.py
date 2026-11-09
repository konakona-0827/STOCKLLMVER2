"""Load versioned, repository-owned research guidance; never grant execution rights."""
import hashlib
import json
import re
from pathlib import Path

from config import CODE_ROOT
from main import save_json

ENTRY_SKILL = 'tw-entry-research'


def skill_policy(context):
    path=CODE_ROOT/'config/research_skills.json'
    policy=json.loads(path.read_text(encoding='utf-8-sig'))
    if type(policy.get('entry_research_enabled')) is not bool:
        raise ValueError('INVALID_ENTRY_RESEARCH_ENABLED')
    override=getattr(context,'entry_skill_enabled',None)
    if override is not None and type(override) is not bool:
        raise ValueError('INVALID_ENTRY_SKILL_OVERRIDE')
    return dict(version=policy['version'],enabled=policy['entry_research_enabled'] if override is None else override,
        source='CODE_CONFIG' if override is None else 'ISOLATED_REPLAY_OVERRIDE',
        config_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def load_entry_research(context, *, skill_root=None):
    context.entry_research_skill=None
    context.entry_research_policy=skill_policy(context)
    context.log_path.parent.mkdir(parents=True,exist_ok=True)
    save_json(context.log_path.with_name('entry_research_policy.json'),context.entry_research_policy)
    if not context.entry_research_policy['enabled']:
        return ''
    symbols = [symbol for symbol,c in context.candidates.items()
               if c.get('opportunity_policy') and (c.get('trade_setups') or c.get('research_only'))]
    if not symbols:
        return ''
    # Only this installed skill is selectable. Market data cannot supply a path
    # or replace instructions, and the original tool allowlist is unchanged.
    path = Path(skill_root or CODE_ROOT/'skills')/ENTRY_SKILL/'SKILL.md'
    raw = path.read_bytes()
    text = raw.decode('utf-8').replace('\r\n','\n')
    frontmatter, separator, body = text[4:].partition('\n---\n')
    version = re.search(r'^  version: ([0-9]+)$',frontmatter,re.MULTILINE)
    if (not text.startswith('---\n') or not separator or not body.strip()
            or not re.search(r'^name: '+re.escape(ENTRY_SKILL)+r'$',frontmatter,re.MULTILINE)
            or version is None):
        raise ValueError('INVALID_ENTRY_RESEARCH_SKILL')
    context.entry_research_skill = dict(name=ENTRY_SKILL,version=version[1],
        sha256=hashlib.sha256(raw).hexdigest(),symbols=symbols)
    context.log_path.parent.mkdir(parents=True,exist_ok=True)
    context.log_path.with_name('entry_research_skill.md').write_bytes(raw)
    save_json(context.log_path.with_name('entry_research_skill.json'),context.entry_research_skill)
    return '\n\n已載入研究 Skill：'+ENTRY_SKILL+'；適用標的：'+', '.join(symbols)+'\n'+body.strip()


def entry_research_report(context, decision, response):
    skill = getattr(context,'entry_research_skill',None)
    if skill is None:
        return None
    decisions = {row['symbol']:row for row in decision.get('decisions',[])}
    support = context.support.summary() if context.support is not None else {}
    reviews = (response.get('pretrade_review') or {}).get('reviews',[])
    entries = []
    for symbol in skill['symbols']:
        c = context.candidates[symbol]
        d = decisions.get(symbol,{})
        research = []
        for call in support.get('calls',[]):
            if call.get('symbol') != symbol:continue
            review = call.get('review') or {}
            research.append(dict(role=call.get('role'),status=call.get('status'),
                packet_id=call.get('packet_id'),state_key=call.get('state_key'),
                **{key:review.get(key) for key in ('verdict','evidence_ids','summary','risks','missing_data')}))
        entries.append(dict(symbol=symbol,quote_status=c.get('quote_status'),
            quote_timestamp=c.get('quote_timestamp'),daily_history_status=c.get('daily_history_status'),
            setup_ids=[s['setup_id'] for s in c.get('trade_setups',[])],
            research_only=bool(c.get('research_only')),decision=d.get('decision'),reason=d.get('reason'),
            independent_research=research,pretrade_reviews=[r for r in reviews if r.get('symbol')==symbol]))
    return dict(skill=skill,model_validation=response.get('validation'),entries=entries,
                tool_calls=len(context.calls),successful_batch_quote_calls=sum(
                    call['tool']=='get_market_data_batch' and call.get('status')=='COMPLETE'
                    for call in context.calls))
