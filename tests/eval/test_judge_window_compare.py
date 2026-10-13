from eval.tools.judge_window_compare import evaluate, summarize

def test_formal_collection_uses_reversed_items_but_restores_original_indices(monkeypatch):
    from eval.tools import judge_window_compare as module
    observed={}
    def judge(client,collection,golden,predicted,**kwargs):
        observed.update(golden=golden,predicted=predicted,kwargs=kwargs)
        kwargs['candidate_pairs_out'].append((0,1))
        return [(0,1)],[],{}
    monkeypatch.setattr(module.run,'_judge_collection',judge)
    row={'id':'a','document_id':'doc','collection':'entity_matches','golden_items':[{'name':'g0'},{'name':'g1'}],
         'predicted_items':[{'name':'p0'},{'name':'p1'}],'labels':[]}
    result=evaluate(row,None,3)
    assert observed['golden'][0]['name']=='g1'
    assert observed['kwargs']['batch_size']==20 and observed['kwargs']['golden_batch_size']==40
    assert result['candidate_pairs']==[(1,0)] and result['matched_pairs']==[(1,0)]

def test_unlabeled_edges_are_not_assumed_correct_and_failed_round_is_not_stable():
    row={'id':'a','document_id':'doc','collection':'entity_matches','labels':[
        {'golden_index':0,'predicted_index':0,'expected_match':True},
        {'golden_index':0,'predicted_index':1,'expected_match':False}]}
    records=[{'id':'a','round':i,'status':'succeeded','candidate_pairs':[(0,0),(9,9)],
              'matched_pairs':[(0,0)],'seconds':1,'http_attempts':1} for i in range(4)]
    records[2].update(status='failed',candidate_pairs=[],matched_pairs=[])
    result=summarize(records,[row])
    assert result['quality_first_round']['labels']==2
    assert result['quality_first_round']['accuracy_including_failures']==1
    assert result['quality_by_round']['2']['failed_labels']==2
    assert result['quality_by_round']['2']['accuracy_including_failures']==0
    assert result['incomplete_stability_windows']==['a']
    assert result['item_order_evaluated_windows']==['a']
    assert not result['item_order_incomplete_windows']

def test_complete_reference_scores_use_maximum_matching_and_report_order_failure():
    row={'id':'a','document_id':'doc','collection':'entity_matches','labels':[],
         'golden_items':[{'name':'a','type':'Device'},{'name':'b','type':'Device'}],
         'predicted_items':[{'name':'a','type':'Device'},{'name':'b','type':'Device'}],
         'expected_pairs':[[0,0],[0,1]]}
    records=[{'id':'a','round':i,'status':'succeeded','candidate_pairs':[(0,0),(1,1)],
              'matched_pairs':[(0,0),(1,1)],'seconds':1,'http_attempts':1} for i in range(4)]
    records[3].update(status='failed')
    result=summarize(records,[row])
    assert result['complete_reviewed_window_scores'][0]['reference_matched']==1
    assert result['complete_reviewed_window_scores'][0]['f1_signed_bias']==.5
    assert result['item_order_incomplete_windows']==['a']
    assert not result['item_order_sensitive_windows']
