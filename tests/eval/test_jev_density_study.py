import hashlib,json
from pathlib import Path
from eval.tools.jev_density_study import summarize,ORDERS

def test_controls_are_independent_of_option_and_language_repeats():
    samples=[{'id':'control','kind':'control','expected_density':'low'},{'id':'real','kind':'real'}]
    records=[{'id':s['id'],'model':'nanojev','language':lang,'order':order,'status':'succeeded','label':'low' if order!=2 else 'high','seconds':.1}
             for s in samples for lang in ['zh','en'] for order in range(3)]
    report=summarize(records,samples)['nanojev']
    assert report['control_first_total']==report['control_first_correct']==1
    assert report['order_drift']==report['order_cases']==4
    assert report['instruction_language_disagreement']==0
    assert report['real_first_labels']=={'real':'low'}

def test_failed_requests_do_not_count_as_stable():
    records=[dict(id='x',model='nanojev',language='zh',order=i,status='succeeded' if i<2 else 'failed',seconds=1,**({'label':'low'} if i<2 else {})) for i in range(3)]
    report=summarize(records,[dict(id='x',kind='control',expected_density='low')])['nanojev']
    assert report['failed']==1
    assert report['order_cases']==0
    assert report['request_seconds_total']==3

def test_frozen_probe_is_contiguous_and_controls_have_count_labels():
    path=Path(__file__).resolve().parents[2]/'test_data/chunk_density/v1/samples.json'
    samples=json.loads(path.read_text(encoding='utf8'))
    assert len({r['id'] for r in samples})==len(samples)==24
    for row in samples:
        start,end=row['view_range']
        assert row['view']==row['text'][start:end]
        assert len(row['view'])<=1200
        assert row['text_sha256']==hashlib.sha256(row['text'].encode()).hexdigest()
        if row['kind']=='control':
            count=row['expected_unique_devices']
            assert row['expected_density']==('low' if count<=3 else 'medium' if count<=10 else 'high')
        else:assert row['expected_density'] is None
    assert all(set(o)=={'low','medium','high'} for o in ORDERS)
