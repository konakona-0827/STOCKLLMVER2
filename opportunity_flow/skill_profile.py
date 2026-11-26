"""Pinned repository Skill over the existing FLOW pipeline; no new broker rights."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from .common import digest

CODE_ROOT=Path(__file__).resolve().parents[1]
NAME='tw-production-decision'
STAGES=('researcher','challenger','pretrade','position_exit')


@dataclass(frozen=True)
class Profile:
    enabled:bool
    version:int
    sha256:str|None
    body:str
    config_sha256:str
    source_text:str=''

    @property
    def metadata(self):
        return dict(name=NAME,version=self.version,enabled=self.enabled,sha256=self.sha256)

    @property
    def cache_identity(self):
        return digest(self.metadata) if self.enabled else None

    def verify(self):
        if load_profile().metadata!=self.metadata:raise ValueError('TRADING_SKILL_CHANGED_RESTART_REQUIRED')

    def guidance(self,stage):
        if stage not in STAGES:raise ValueError('UNKNOWN_TRADING_SKILL_STAGE')
        self.verify()
        return '\n\n已載入 '+NAME+'；階段 '+stage+'；沿用 host schema 與原執行器。\n'+self.body if self.enabled else ''

    def request(self,request,stage):
        result=deepcopy(request)
        if self.enabled:result['instructions']=result.get('instructions','')+self.guidance(stage)
        return result


def load_profile():
    config_file=CODE_ROOT/'config/trading_skill.json';raw=config_file.read_bytes();p=json.loads(raw.decode('utf-8-sig'))
    if set(p)!={'enabled','name','version','expected_sha256'} or type(p['enabled']) is not bool or p['name']!=NAME or type(p['version']) is not int or p['version']<1:
        raise ValueError('INVALID_TRADING_SKILL_CONFIG')
    override=os.getenv('FLOW_TRADING_SKILL')
    if override not in (None,'on','off'):raise ValueError('INVALID_TRADING_SKILL_OVERRIDE')
    enabled=p['enabled'] if override is None else override=='on'
    if not enabled:return Profile(False,p['version'],None,'',hashlib.sha256(raw).hexdigest())
    source=(CODE_ROOT/'skills'/NAME/'SKILL.md').read_bytes().replace(b'\r\n',b'\n');sha=hashlib.sha256(source).hexdigest()
    if sha!=p['expected_sha256']:raise ValueError('TRADING_SKILL_HASH_MISMATCH')
    text=source.decode('utf-8').replace('\r\n','\n')
    if not text.startswith('---\n') or '\nname: '+NAME+'\n' not in text.split('\n---\n',1)[0]+'\n':
        raise ValueError('INVALID_TRADING_SKILL_FRONTMATTER')
    body=text.split('\n---\n',1)[1].strip()
    if '\n  version: '+str(p['version'])+'\n' not in text.split('\n---\n',1)[0]+'\n':raise ValueError('TRADING_SKILL_VERSION_MISMATCH')
    return Profile(True,p['version'],sha,body,hashlib.sha256(raw).hexdigest(),source.decode('utf-8'))


def validate_review_profile(review):
    profile=load_profile();actual=review.get('trading_skill')
    if profile.enabled and actual!=profile.metadata:raise ValueError('RESEARCH_TRADING_SKILL_MISMATCH')
    if not profile.enabled and actual and actual.get('enabled'):raise ValueError('RESEARCH_TRADING_SKILL_MISMATCH')
    return profile
