import pytest
from eval.tools.entity_count_study import decode,summarize

def test_threshold_questions_reject_logically_inconsistent_answers():
    with pytest.raises(ValueError,match='inconsistent'):
        decode({'gt20':{'choice':'no'},'gt40':{'choice':'yes'}},'thresholds_en')
    assert decode({'gt20':{'choice':'yes'},'gt40':{'choice':'no'}},'thresholds_en')=='21to40'

def test_failed_predictions_are_not_counted_correct_and_under_split_is_reported():
    rows=[dict(id='a',expected_bucket='gt40'),dict(id='b',expected_bucket='le20')]
    records=[dict(id='a',variant='direct_zh',status='succeeded',bucket='le20',seconds=1),dict(id='b',variant='direct_zh',status='failed',seconds=2)]
    report=summarize(records,rows)['direct_zh']
    assert report['total']==2 and report['failed']==1 and report['correct']==0
    assert report['under_split']==1 and report['accuracy']==0
    assert report['confusion']['gt40']['le20']==1
