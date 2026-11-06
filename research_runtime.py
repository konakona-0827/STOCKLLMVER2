"""Account for research without token quotas; finish truncated model steps."""
import asyncio
import json
import os
from pathlib import Path

from config import now
from main import save_json

USAGE_KEYS=('input_tokens','output_tokens','requests','cached_input_tokens',
            'cache_write_tokens','reasoning_tokens')


def totals(rows):
    return {key:sum(row.get(key,0) or 0 for row in rows) for key in USAGE_KEYS}


class ResponseRecorder:
    def __init__(self,path,role,rows=None,stopped=lambda:False,on_incomplete=None):
        self.path=Path(path);self.role=role;self.rows=rows if rows is not None else []
        self.stopped=stopped;self.accounting_complete=True
        self.on_incomplete=on_incomplete
        self.accounting_path=self.path.with_suffix('.accounting.json')

    def progress(self,status):
        if self.path.parent.parent.name=='runs':
            directory=self.path.parent.parent.parent
        else:directory=self.path.parent
        save_json(directory/'research_activity.json',dict(pid=os.getpid(),at=now().isoformat(),
            run_id=self.path.parent.name,role=self.role,status=status))
        save_json(self.accounting_path,dict(complete=self.accounting_complete and status not in
            ('REQUEST_STARTED','OUTPUT_TRUNCATED_RETRYING'),status=status))

    def install(self,client):
        create=client.responses.create
        async def recorded_create(*args,**kwargs):
            options=dict(kwargs);instructions=options.get('instructions') or ''
            while True:
                if self.stopped():
                    self.progress('STOPPED_BEFORE_REQUEST')
                    raise RuntimeError('RESEARCH_STOPPED_BY_USER')
                self.progress('REQUEST_STARTED')
                try:
                    response=await create(*args,**options)
                except BaseException:
                    self.accounting_complete=False;self.progress('REQUEST_FAILED_OR_CANCELLED')
                    raise
                usage=getattr(response,'usage',None)
                if usage is None:self.accounting_complete=False
                else:
                    detail=getattr(usage,'input_tokens_details',None)
                    output=getattr(usage,'output_tokens_details',None)
                    self.rows.append(dict(input_tokens=usage.input_tokens,output_tokens=usage.output_tokens,requests=1,
                        cached_input_tokens=getattr(detail,'cached_tokens',0) or 0,
                        cache_write_tokens=getattr(detail,'cache_write_tokens',0) or 0,
                        reasoning_tokens=getattr(output,'reasoning_tokens',0) or 0,role=self.role,
                        response_id=response.id,response_status=response.status,
                        input_characters=len(json.dumps(options.get('input',''),ensure_ascii=False,default=str))))
                    save_json(self.path,self.rows)
                self.progress('RESPONSE_RECEIVED')
                reason=getattr(getattr(response,'incomplete_details',None),'reason',None)
                if response.status!='incomplete' or reason!='max_output_tokens':return response
                if self.on_incomplete:self.on_incomplete(response)
                # Retry this model step with the exact same tool results. No
                # SDK tool, broker query or order is replayed by this wrapper.
                previous=getattr(response,'max_output_tokens',None)
                if type(previous) is not int or previous<=0:
                    previous=options.get('max_output_tokens')
                if type(previous) is not int or previous<=0:
                    previous=getattr(usage,'output_tokens',0) or 16384
                options['max_output_tokens']=previous*2
                options['instructions']=instructions+'\n前次回覆因輸出長度未完成。請回傳完整指定結構，精簡敘述但保留所有必要欄位與候選；不要重做已提供的工具研究。'
                self.progress('OUTPUT_TRUNCATED_RETRYING')
        client.responses.create=recorded_create
        return self
