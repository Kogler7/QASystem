import json
import logging

import pytest

from algorithm.knowledge_pipeline.entity_count_chunking import refine_chunks


@pytest.mark.parametrize('count,parts', [(0,1),(20,1),(21,2),(40,2),(41,3)])
def test_count_band_boundaries_and_one_estimate_per_original(count,parts):
    text=''.join(chr(0x4e00+i) for i in range(300))
    calls=[]
    def estimate(value):
        calls.append(value)
        return {'estimated_count':count}
    result=refine_chunks(text,[{'text':text,'range':[0,len(text)]}],estimate)
    assert len(result)==parts
    assert calls==[text]


@pytest.mark.parametrize('bucket,parts', [('21to40',2),('gt40',3)])
def test_global_offsets_overlap_metadata_and_full_source_coverage(bucket,parts):
    text=''.join(chr(0x4e00+i) for i in range(301))
    content='PREFIX'+text+'SUFFIX'
    original={'text':text,'range':[6,307],'metadata':{'document_id':'doc-a','page':3},'custom':'preserved'}
    result=refine_chunks(content,[original],lambda _: {'bucket':bucket,'confidence':.9},overlap=50)
    assert len(result)==parts
    assert result[0]['range'][0]==6 and result[-1]['range'][1]==307
    reconstructed=result[0]['text']
    for before,after in zip(result,result[1:]):
        assert before['range'][1]-after['range'][0]==50
        assert before['text'][-50:]==after['text'][:50]
        reconstructed+=after['text'][50:]
    assert reconstructed==text
    assert all(content[a:b]==r['text'] for r in result for a,b in [r['range']])
    assert all(r['metadata']==original['metadata'] and r['custom']=='preserved' for r in result)
    result[0]['metadata']['page']=9
    assert original['metadata']['page']==result[1]['metadata']['page']==3


@pytest.mark.parametrize('text', ['# Table\n```json\n{"rows":[1,2]}\n```',
                                'prose {"rows":[1,2]} tail',
                                '[1,2,3]',
                                '|a|b|\n|---|---|\n|1|2|',
                                '~~~\nunfinished'])
def test_structures_preserved_and_actual_log_is_one(text,caplog):
    with caplog.at_level(logging.INFO):
        result=refine_chunks(text,[{'text':text,'range':[0,len(text)]}],lambda _: {'bucket':'gt40'})
    assert result==[{'text':text,'range':[0,len(text)]}]
    record=json.loads(caplog.records[-1].args[0])
    assert record['bucket']=='gt40'
    assert record['planned_parts']==3 and record['actual_parts']==1
    assert record['status']=='preserved_structured'
    assert record['child_sizes']==[len(text)]
    assert text not in caplog.text


def test_estimator_failure_preserves_original_and_logs_safe_reason(caplog):
    text='SOURCE_SENTINEL'
    def fail(_):
        raise RuntimeError('Authorization: SECRET_KEY '+text)
    with caplog.at_level(logging.INFO):
        result=refine_chunks(text,[{'text':text,'range':[0,len(text)],'metadata':{'doc':'x'}}],fail)
    assert result[0]['text']==text
    record=json.loads(caplog.records[-1].args[0])
    assert record['status']=='estimator_fallback'
    assert record['reason']=='RuntimeError'
    assert record['actual_parts']==1 and record['planned_parts'] is None
    assert 'SECRET_KEY' not in caplog.text and text not in caplog.text


@pytest.mark.parametrize('prediction', [{'bucket':'bad'},{'estimated_count':-1},{'estimated_count':True},None])
def test_invalid_predictions_fall_back(prediction):
    result=refine_chunks('abc',[{'text':'abc','range':[0,3]}],lambda _:prediction)
    assert result==[{'text':'abc','range':[0,3]}]


@pytest.mark.parametrize('length', [1,2,3,5,12])
def test_short_blocks_reduce_overlap_without_losing_progress(length,caplog):
    text=''.join(chr(0x4e00+i) for i in range(length))
    with caplog.at_level(logging.INFO):
        result=refine_chunks(text,[{'text':text,'range':[0,length]}],lambda _: {'bucket':'gt40'},50)
    assert len(result)==min(3,length)
    record=json.loads(caplog.records[-1].args[0])
    assert record['actual_parts']==len(result)
    assert record['requested_overlap']==50
    assert record['effective_overlap']<50
    assert result[0]['range'][0]==0 and result[-1]['range'][1]==length
    assert all(r['text'] for r in result)
    assert all(a['range'][0]<b['range'][0] and a['range'][1]<b['range'][1] for a,b in zip(result,result[1:]))


def test_invalid_source_ranges_are_rejected():
    with pytest.raises(ValueError,match='source range'):
        refine_chunks('abcd',[{'text':'abc','range':[1,4]}],lambda _: {'bucket':'le20'})


def test_inline_scalar_citation_does_not_disable_plain_text_refinement():
    text='A paragraph [1] with a citation. '*20
    result=refine_chunks(text,[{'text':text,'range':[0,len(text)]}],lambda _: {'bucket':'gt40'})
    assert len(result)==3
