"""Estimate a count interval through the configured SystemOne decision service."""
from __future__ import annotations
import math
from external_services.clef import ClefFlashClient
from external_services.nanojev import NanoJevServiceClient

ENTITY_COUNT_QUESTION_ZH={
    'type':'choice',
    'instructions':'估计从当前完整文本中可抽取的不同实体数量。同一对象重复出现只算一次；实体包括具体机构、人员、设备、地点、文档、任务、业务规则等。属性、字段名、年份及重复证据不单独算实体。不要统计行数或字符数，不推断未提供的正文。仅判断数量所属区间。',
    'criteria':{'le20':'预计0至20个不同实体（含20）','21to40':'预计21至40个不同实体（含40）','gt40':'预计41个或以上不同实体'}}

ENTITY_COUNT_QUESTION={
    'type':'choice',
    'instructions':'Estimate how many distinct entities could be extracted from the entire supplied passage. Count real identifiable organizations, persons, equipment, places, documents, tasks and business rules. Count the same referent only once. Attributes, field names, years and repeated evidence are not separate entities. Count neither lines nor characters. Do not infer missing document content. Choose only the count interval.',
    'criteria':{'le20':'0 through 20 distinct entities, inclusive','21to40':'21 through 40 distinct entities, inclusive','gt40':'41 or more distinct entities'}}

def estimate_entities(text: str, *, model: str = 'clef-flash', service: dict | None = None) -> dict:
    if service is not None:
        client=ClefFlashClient(service=service)
        model=service['model']
    elif model == 'clef-flash':
        client=ClefFlashClient(model=model)
    elif model == 'nanojev':
        client=NanoJevServiceClient()
    else:
        raise ValueError('unsupported entity count model')
    response=client.systemone({'state':text,'questions':{'entity_count':ENTITY_COUNT_QUESTION}})
    answer=response['answers']['entity_count'];bucket=answer['choice']
    if bucket not in ENTITY_COUNT_QUESTION['criteria']:raise ValueError('invalid count interval')
    result={'bucket':bucket,'model':model}
    if service is not None:
        result.update(service_id=service['service_id'], config_revision=service.get('revision'))
    if answer.get('confidence') is not None:
        confidence=float(answer['confidence'])
        if not math.isfinite(confidence) or not 0<=confidence<=1:raise ValueError('invalid confidence')
        result['confidence']=confidence
    return result
