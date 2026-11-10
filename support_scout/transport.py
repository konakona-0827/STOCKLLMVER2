"""Lossless model transport; the signed full audit packet stays unchanged."""
from copy import deepcopy

from token_budget import compact


def _parts(pointer):
    return [x.replace('~1','/').replace('~0','~') for x in pointer.removeprefix('#/').split('/')]


def _get(value,pointer):
    for part in _parts(pointer):value=value[int(part)] if isinstance(value,list) else value[part]
    return value


def _set(value,pointer,item):
    parts=_parts(pointer);parent=value
    for part in parts[:-1]:parent=parent[int(part)] if isinstance(parent,list) else parent[part]
    key=int(parts[-1]) if isinstance(parent,list) else parts[-1];parent[key]=item


def model_packet(packet):
    if '_transport' in packet:raise ValueError('ALREADY_ENCODED_SUPPORT_PACKET')
    wire=deepcopy(packet);references={};core=wire['core']
    if wire.get('tasks')==core.get('agent_tasks'):
        references['#/tasks']='#/core/agent_tasks';wire['tasks']={'$ref':'#/core/agent_tasks'}
    if core.get('entry_context') is not None:
        for i,evidence in enumerate(core.get('evidence',[])):
            if evidence.get('kind')=='ENTRY_CONTEXT' and evidence.get('values')==core['entry_context']:
                pointer=f'#/core/evidence/{i}/values';references[pointer]='#/core/entry_context'
                evidence['values']={'$ref':'#/core/entry_context'}
    encoded=compact(wire);tables=[]
    def locate(original,current,path):
        if isinstance(original,list) and isinstance(current,dict) and set(current)=={'columns','rows'}:
            tables.append(path);return
        if isinstance(original,dict):
            for key,value in original.items():locate(value,current[key],path+'/'+str(key).replace('~','~0').replace('/','~1'))
        elif isinstance(original,list):
            for i,value in enumerate(original):locate(value,current[i],path+'/'+str(i))
    locate(wire,encoded,'#')
    encoded['_transport']=dict(version='support-model-wire-v1',references=references,tables=tables)
    return encoded


def expand_packet(wire):
    result=deepcopy(wire);metadata=result.pop('_transport')
    if metadata['version']!='support-model-wire-v1':raise ValueError('UNKNOWN_SUPPORT_TRANSPORT')
    for pointer in sorted(metadata['tables'],key=len,reverse=True):
        table=_get(result,pointer)
        _set(result,pointer,[dict(zip(table['columns'],row,strict=True)) for row in table['rows']])
    for pointer,target in metadata['references'].items():
        if _get(result,pointer)!={'$ref':target}:raise ValueError('INVALID_SUPPORT_REFERENCE')
        _set(result,pointer,deepcopy(_get(result,target)))
    return result
