"""Engine registrations and process contracts; no model-specific deployment ENV."""
from __future__ import annotations
import math
import os
from pathlib import Path
import re
import shlex
import sys

NATIVE_OPTIONS = {'dtype', 'max_length', 'max_request_bytes', 'max_questions', 'max_options',
                  'max_total_options', 'max_queue_size', 'cpu_threads', 'api_key_env'}
WORKER_OPTIONS = {'served_model', 'extra_args'}
ENGINE_CONTRACT = {
    'vllm': {'adapters': ['chat'], 'devices': ['gpu', 'worker']},
    'native': {'adapters': ['clef', 'nanojev'], 'devices': ['cpu', 'gpu']},
}
BUILTIN_MODELS = {
    'clef-flash': dict(engine='native', adapter='clef', manager='ops', model_dir='clef-flash', port='8188',
                       device='', utilization='0.25', engine_options={'dtype': 'bfloat16', 'max_length': '8192'}),
    'nanojev': dict(engine='native', adapter='nanojev', manager='ops', model_dir='nanojev', port='8189',
                    device='', utilization='0.25', engine_options={'dtype': 'bfloat16', 'max_length': '8192', 'cpu_threads': '4'}),
    'mineru-vlm': dict(engine='vllm', adapter='chat', manager='backend-worker', model_dir='mineru/vlm', port='8100',
                       device='worker', utilization='0.82', max_num_seqs='32', engine_options={}),
}

REGISTRY_CONTRACT = {
    'engines': ENGINE_CONTRACT, 'native_options': sorted(NATIVE_OPTIONS), 'worker_options': sorted(WORKER_OPTIONS),
    'builtin_models': BUILTIN_MODELS, 'registry_fields': ['settings', 'models', 'services', 'routes'],
    'managers': ['ops', 'backend-worker'], 'internal_port': 8000,
    'consumers': ['docker/configure.py', 'backend/app/execution/supervisor.py', 'clef_service/app.py',
                  'nanojev_service/app.py', 'external_services/model_routes.py'],
}

_SELECTED = None


def service_name(model_id):
    return 'model_' + re.sub(r'[^a-z0-9]+', '_', model_id.lower()).strip('_')


def local_model_endpoint(model_id, record):
    if record.get('manager', 'ops') != 'ops':
        raise ValueError('Worker 引擎不是独立常驻模型服务')
    adapter = record.get('adapter', 'chat')
    base = 'http://' + service_name(model_id) + ':8000'
    return {'base_url': base + ('/v1' if adapter == 'chat' else ''),
            'adapter': adapter, 'model': 'clef-flash' if adapter == 'clef' else 'nanojev' if adapter == 'nanojev' else model_id}


def physical_gpu(record):
    value = record.get('device', '')
    return value[4:] if value.startswith('gpu:') else ''


def model_path(record, root=Path('/models')):
    value = record.get('model_dir', '')
    target = (Path(root) / value).resolve()
    if not value or Path(value).is_absolute() or not target.is_relative_to(Path(root).resolve()):
        raise ValueError('权重路径必须位于共同模型根目录中')
    return target


def validate_record(model_id, record, *, root=None, check_files=False):
    engine = record.get('engine', 'vllm')
    adapter = record.get('adapter', 'chat')
    manager = record.get('manager', 'ops')
    if engine not in ENGINE_CONTRACT or adapter not in ENGINE_CONTRACT[engine]['adapters']:
        raise ValueError('未登记推理引擎或启动适配器')
    if manager not in {'ops', 'backend-worker'}:
        raise ValueError('未知模型启动管理者')
    if manager == 'backend-worker' and (engine != 'vllm' or model_id != 'mineru-vlm'):
        raise ValueError('仅 MinerU 解析引擎由 Backend worker 管理')
    device = record.get('device', '')
    if device == 'worker':
        if manager != 'backend-worker': raise ValueError('worker 设备仅适用于解析引擎')
    elif device == 'cpu':
        if engine != 'native': raise ValueError('当前 vLLM 镜像不支持 CPU 推理')
    elif not (physical_gpu(record).isdigit() or re.fullmatch(r'GPU-[A-Za-z0-9-]+', physical_gpu(record))):
        raise ValueError('必须显式选择 cpu 或 gpu:<物理编号>，不会自动回退')
    options = record.get('engine_options', {})
    allowed = NATIVE_OPTIONS if engine == 'native' else WORKER_OPTIONS if manager == 'backend-worker' else set()
    if set(options) - allowed: raise ValueError('引擎参数未登记')
    if any(key.startswith('max_') and int(value) < 1 for key, value in options.items()):
        raise ValueError('原生推理限制必须为正整数')
    if options.get('api_key_env') and not re.fullmatch(r'[A-Z][A-Z0-9_]*', options['api_key_env']):
        raise ValueError('服务凭据必须引用已有环境变量名')
    extra = shlex.split(str(options.get('extra_args', '')))
    blocked = {'--model', '--host', '--port', '--served-model-name', '--gpu-memory-utilization', '--max-num-seqs', '--api-key'}
    if any(token.split('=')[0] in blocked for token in extra): raise ValueError('额外参数不得覆盖集中模型记录或包含凭据')
    ratio = float(record.get('utilization', '0.9'))
    if not math.isfinite(ratio) or not 0 < ratio < 1: raise ValueError('显存预算必须在 0 与 1 之间')
    if not 1 <= int(record.get('port', '8000')) <= 65535: raise ValueError('服务端口无效')
    if record.get('bind', '127.0.0.1') not in {'127.0.0.1', '0.0.0.0'}: raise ValueError('监听地址无效')
    if root is not None:
        path = model_path(record, root)
        if not path.is_dir(): raise ValueError('权重目录不存在')
        if check_files:
            if adapter == 'clef': required = ['joint_schema_model.py', 'joint_head.safetensors', 'joint_head_config.json',
                'config.json', 'tokenizer.json', 'tokenizer_config.json', 'processor_config.json']
            elif adapter == 'nanojev': required = ['source/scripts/predict_toy_decisions.py', 'source/scripts/train_toy_decisions.py', 'best.safetensors', 'config.json']
            else: required = ['config.json']
            if any(not (path / name).is_file() for name in required): raise ValueError('必要权重或加载文件不完整')
            if adapter == 'nanojev' and any(not (path / name).is_dir() or not any((path / name).iterdir()) for name in ('backbone_config', 'tokenizer')):
                raise ValueError('NanoJev 缺少 backbone/tokenizer 文件')
            if (path / 'model.safetensors.index.json').is_file():
                import json
                index = json.loads((path / 'model.safetensors.index.json').read_text())
                shards = set(index.get('weight_map', {}).values())
                if not shards or any(not (path / name).is_file() or not (path / name).resolve().is_relative_to(path) for name in shards):
                    raise ValueError('模型分片权重不完整')
            if not any(path.rglob('*.safetensors')) and not any(path.rglob('*.bin')):
                raise ValueError('模型目录没有本地权重文件')


def startup_command(model_id, record):
    if record.get('manager', 'ops') != 'ops': raise ValueError('禁止 ops 重复启动 Backend 管理的引擎')
    adapter = record.get('adapter', 'chat')
    if adapter in {'clef', 'nanojev'}:
        return ['python', '-m', adapter + '_service', '--record', model_id, '--container']
    return ['vllm', 'serve', '/models/' + record['model_dir'], '--served-model-name', model_id,
            '--max-model-len', record['context'], '--gpu-memory-utilization', record['utilization'],
            '--max-num-seqs', record['max_num_seqs'], '--max-num-batched-tokens', record['max_num_batched_tokens'],
            '--dtype', record['dtype'], '--host', '0.0.0.0', '--port', '8000']


def selected_native(adapter):
    from docker.configuration import read_registry
    records = read_registry()['models']
    if _SELECTED is not None:
        if _SELECTED not in records: raise ValueError('服务模型记录不存在')
        record = records[_SELECTED]
        if not record.get('enabled') or record.get('adapter') != adapter:
            raise ValueError('服务模型记录未启用或适配器不匹配')
        return record
    matches = [r for r in records.values() if r.get('enabled') and r.get('adapter') == adapter]
    if len(matches) > 1: raise ValueError('多个同类服务必须通过 --record 明确选择')
    return matches[0] if matches else None


def native_main(adapter):
    import argparse
    import logging
    global _SELECTED
    parser = argparse.ArgumentParser()
    parser.add_argument('--record', required=True)
    parser.add_argument('--container', action='store_true')
    args = parser.parse_args()
    _SELECTED = args.record
    record = selected_native(adapter)
    validate_record(args.record, record, root=Path('/models'), check_files=True)
    if record.get('manager', 'ops') != 'ops': raise ValueError('模型不是 ops 独立服务')
    os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    os.environ['CUDA_VISIBLE_DEVICES'] = physical_gpu(record)
    if physical_gpu(record):
        import torch
        if not torch.cuda.is_available(): raise RuntimeError('指定 GPU 不可用，不会回退到 CPU')
        torch.cuda.set_per_process_memory_fraction(float(record['utilization']), 0)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    import uvicorn
    uvicorn.run(adapter + '_service.app:app', host='0.0.0.0' if args.container else record['bind'],
                port=8000 if args.container else int(record['port']), workers=1, access_log=False)


def worker_record():
    from docker.configuration import read_registry
    record = read_registry()['models'].get('mineru-vlm')
    if record is not None and record.get('manager') != 'backend-worker':
        raise ValueError('MinerU 只能由 Backend worker 启动管理')
    return record
