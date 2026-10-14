import json
from pathlib import Path
import struct
import sys
import types

import pytest

from qasystem_config import mineru_adapter as adapter


@pytest.fixture
def model_settings(tmp_path):
    base = tmp_path / 'models'
    (base / '.locks').mkdir(parents=True)
    values = dict(home=str(tmp_path/'home'), base_dir=str(base), source='local',
        small_backend='onnx', vlm_engine='llama-cpp', declaration_version='test', revision=7)
    for repo, names in adapter.required_repositories(values, 'standard').items():
        for name in names:
            path = base/repo/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'GGUF'+struct.pack('<IQQ', 3, 1, 1)+b'fixture-payload'*5 if path.suffix=='.gguf' else b'fixture-payload')
        (base/repo/'.mineru_complete').touch()
    return values


def prepare(tmp_path, values, inherited=None):
    work = tmp_path/'job'
    work.mkdir()
    return adapter.prepare_runtime(values, 'standard', work, inherited or {})


def fake_loaded_config(monkeypatch, plan_path, *, wrong_source=False, wrong_file=False):
    plan=json.loads(plan_path.read_text())
    model=json.loads(Path(plan['config_path']).read_text())['model']
    settings=types.SimpleNamespace(model=types.SimpleNamespace(source='modelscope' if wrong_source else 'local',
        base_dir=model['base_dir'], small_backend=model['small_backend'],
        vlm=types.SimpleNamespace(engine=model['vlm']['engine'], server_url='')),
        llm_aided=types.SimpleNamespace(features=types.SimpleNamespace(title_leveling=False, cross_page_table_cell_merge=False)))
    module=types.ModuleType('mineru.config')
    module.config=settings
    module.get_config_file_path=lambda: 'old-config.yaml' if wrong_file else plan['config_path']
    monkeypatch.setitem(sys.modules, 'mineru.config', module)


def test_legacy_yaml_and_unknown_env_cannot_override_and_parent_is_unchanged(model_settings, tmp_path):
    home=Path(model_settings['home']);home.mkdir()
    old=home/'config.yaml'
    old.write_text('model:\n  source: modelscope\n  base_dir: ./wrong\nllm_aided:\n  features:\n    title_leveling: true\n')
    prior=old.read_bytes()
    inherited={'MINERU_MODEL_SOURCE':'auto','MINERU_MODEL_BASE_DIR':'./wrong',
        'MINERU_CONFIG':str(old),'MINERU_LLM_AIDED_FEATURES_TITLE_LEVELING':'true','CUDA_VISIBLE_DEVICES':'4'}
    original=dict(inherited)
    plan_path, child, stamp=prepare(tmp_path, model_settings, inherited)
    plan=json.loads(plan_path.read_text());generated=json.loads(Path(plan['config_path']).read_text())
    assert generated['model']['source']=='local'
    assert generated['model']['base_dir']==model_settings['base_dir']
    assert generated['model']['vlm']['server_url']==''
    assert generated['llm_aided']['features']==dict(title_leveling=False, cross_page_table_cell_merge=False)
    assert {key for key in child if key.startswith('MINERU_')}=={'MINERU_CONFIG','MINERU_HOME'}
    assert child['CUDA_VISIBLE_DEVICES']=='4'
    assert inherited==original and old.read_bytes()==prior
    assert stamp['revision']==7 and stamp['legacy_config_ignored'] and len(stamp['fingerprint'])==64


@pytest.mark.parametrize('key,value', [('home','./data'),('base_dir','./models'),('base_dir','/models/${OTHER}'),
    ('source','auto'),('source','modelscope'),('source','huggingface'),('small_backend','auto'),('vlm_engine','auto')])
def test_invalid_or_network_configuration_is_rejected(model_settings, key, value):
    with pytest.raises(ValueError):adapter.validate_settings({**model_settings,key:value})


def test_marker_without_real_weights_never_passes(model_settings):
    base=Path(model_settings['base_dir'])
    (base/'MinerU2.5-Pro-2605-1.2B-GGUF'/adapter.GGUF_FILES[0]).unlink()
    report=adapter.model_report(model_settings,'standard')
    assert not report['ok'] and any(adapter.GGUF_FILES[0] in item for item in report['missing'])


def test_onnx_named_file_resources_do_not_need_completion_marker(model_settings):
    (Path(model_settings['base_dir'])/'MinerU-4_models_onnx'/'.mineru_complete').unlink()
    assert adapter.model_report(model_settings,'basic')['ok']


def test_empty_onnx_is_detected(model_settings):
    path=Path(model_settings['base_dir'])/'MinerU-4_models_onnx'/adapter.ONNX_FILES[0]
    path.write_bytes(b'')
    report=adapter.model_report(model_settings,'basic')
    assert not report['ok'] and adapter.ONNX_FILES[0] in report['missing'][0]


def test_bad_gguf_header_is_detected(model_settings):
    path=Path(model_settings['base_dir'])/'MinerU2.5-Pro-2605-1.2B-GGUF'/adapter.GGUF_FILES[0]
    path.write_bytes(b'not-gguf-file-but-long-enough')
    assert not adapter.model_report(model_settings,'standard')['ok']


def test_missing_writable_lock_mount_is_detected(model_settings):
    (Path(model_settings['base_dir'])/'.locks').rmdir()
    report=adapter.model_report(model_settings,'standard',check_lock=True)
    assert not report['ok'] and any('.locks' in item for item in report['invalid'])


def test_flash_requires_no_models_or_receipts(model_settings):
    values={**model_settings,'base_dir':str(Path(model_settings['base_dir'])/'absent')}
    assert adapter.model_report(values,'flash',check_lock=True)['ok']


@pytest.mark.parametrize('wrong_source,wrong_file', [(True,False),(False,True)])
def test_loaded_library_config_must_match_controlled_config(model_settings,tmp_path,monkeypatch,wrong_source,wrong_file):
    plan,_,_=prepare(tmp_path,model_settings)
    fake_loaded_config(monkeypatch,plan,wrong_source=wrong_source,wrong_file=wrong_file)
    with pytest.raises(ValueError):adapter.verify_loaded_configuration(plan)


def test_loaded_config_records_actual_configuration(model_settings,tmp_path,monkeypatch):
    plan,_,stamp=prepare(tmp_path,model_settings)
    fake_loaded_config(monkeypatch,plan)
    assert adapter.verify_loaded_configuration(plan)==stamp


def test_tampered_generated_file_is_rejected_before_import(model_settings,tmp_path):
    plan,_,_=prepare(tmp_path,model_settings)
    path=Path(json.loads(plan.read_text())['config_path']);path.write_text('{}')
    with pytest.raises(ValueError,match='指纹'):adapter.verify_loaded_configuration(plan)


def test_raw_safetensors_missing_shards_and_truncation_are_detected(model_settings):
    values={**model_settings,'vlm_engine':'vllm'}
    folder=Path(values['base_dir'])/'MinerU2.5-Pro-2605-1.2B';folder.mkdir()
    for name in ('config.json','tokenizer_config.json'): (folder/name).write_text('{}')
    (folder/'.mineru_complete').touch()
    header=json.dumps({'weight':{'dtype':'F32','shape':[1],'data_offsets':[0,4]}}).encode()
    shard=folder/'weights.safetensors';shard.write_bytes(struct.pack('<Q',len(header))+header+b'1234')
    index=folder/'model.safetensors.index.json'
    index.write_text(json.dumps({'weight_map':{'weight':'weights.safetensors'}}))
    assert adapter.model_report(values,'standard')['ok']
    shard.write_bytes(shard.read_bytes()[:-1])
    assert not adapter.model_report(values,'standard')['ok']
    index.write_text(json.dumps({'weight_map':{'weight':'../outside.safetensors'}}))
    assert not adapter.model_report(values,'standard')['ok']


def test_health_uses_required_files_and_drops_retired_pipeline(model_settings,monkeypatch):
    from backend.app import health
    monkeypatch.setattr(health,'is_remote_only',lambda:False)
    monkeypatch.setattr(adapter,'settings',lambda:dict(model_settings))
    original=health.config_getenv
    monkeypatch.setattr(health,'config_getenv',lambda key,*args: 'standard' if key=='MINERU4_TIER' else original(key,*args))
    missing=Path(model_settings['base_dir'])/'MinerU-4_models_onnx'/adapter.ONNX_FILES[0]
    missing.unlink()
    checks=health.runtime_checks()
    assert 'mineru_pipeline' not in checks
    assert not checks['mineru4']['ok'] and any(adapter.ONNX_FILES[0] in item for item in checks['mineru4']['missing'])


def test_ops_rejects_relative_model_path_before_saving(tmp_path,monkeypatch):
    from docker.configure import Configuration
    monkeypatch.setattr(Configuration,'refresh_live',lambda self:None)
    config=Configuration(tmp_path)
    config.values['MINERU_MODEL_BASE_DIR']='./models/mineru4'
    assert any('MINERU_MODEL_BASE_DIR' in error and '绝对路径' in error for error in config.validate())


@pytest.mark.parametrize('source,owner',[('huggingface','opendatalab'),('modelscope','OpenDataLab')])
def test_download_prepares_v4_and_marks_only_after_validation(model_settings,monkeypatch,source,owner):
    from docker import download_models
    calls=[]
    def download(**kwargs):
        calls.append(kwargs.get('repo_id') or kwargs.get('model_id'))
        assert not (Path(kwargs['local_dir'])/'.mineru_complete').exists()
    monkeypatch.setitem(sys.modules,'huggingface_hub',types.SimpleNamespace(snapshot_download=download))
    monkeypatch.setitem(sys.modules,'modelscope',types.SimpleNamespace(snapshot_download=download))
    report=download_models._download_mineru(source,model_settings,'basic')
    assert calls==[owner+'/MinerU-4_models_onnx'] and report['ok']
    assert (Path(model_settings['base_dir'])/'MinerU-4_models_onnx'/'.mineru_complete').exists()


def test_failed_download_never_creates_completion_receipt(model_settings,monkeypatch):
    from docker import download_models
    def incomplete(**kwargs):
        (Path(kwargs['local_dir'])/adapter.GGUF_FILES[0]).write_bytes(b'truncated')
    monkeypatch.setitem(sys.modules,'huggingface_hub',types.SimpleNamespace(snapshot_download=incomplete))
    # Use standard resources; the callback must only mutate the GGUF directory.
    def download(**kwargs):
        if Path(kwargs['local_dir']).name.endswith('-GGUF'):incomplete(**kwargs)
    monkeypatch.setitem(sys.modules,'huggingface_hub',types.SimpleNamespace(snapshot_download=download))
    with pytest.raises(RuntimeError,match='incomplete'):
        download_models._download_mineru('huggingface',model_settings,'standard')
    for repo in adapter.required_repositories(model_settings,'standard'):
        assert not (Path(model_settings['base_dir'])/repo/'.mineru_complete').exists()


def test_provider_contract_change_requires_a_new_declaration(monkeypatch):
    from qasystem_config import config_contract
    monkeypatch.setattr(adapter,'ONNX_FILES',(*adapter.ONNX_FILES,'unregistered.onnx'))
    errors=config_contract.check(Path(__file__).resolve().parents[2])
    assert any('MinerU' in error and '新声明版本' in error for error in errors)
