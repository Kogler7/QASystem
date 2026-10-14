"""Ops model startup command generation; registry lives in qasystem_config."""
def startup_command(model_id, record):
    if record.get('manager', 'ops') != 'ops': raise ValueError('禁止 ops 重复启动 Backend 管理的引擎')
    adapter = record.get('adapter', 'chat')
    if adapter in {'clef', 'nanojev'}:
        return ['python', '-m', adapter + '_service', '--record', model_id, '--container']
    return ['vllm', 'serve', '/models/' + record['model_dir'], '--served-model-name', model_id,
            '--max-model-len', record['context'], '--gpu-memory-utilization', record['utilization'],
            '--max-num-seqs', record['max_num_seqs'], '--max-num-batched-tokens', record['max_num_batched_tokens'],
            '--dtype', record['dtype'], '--host', '0.0.0.0', '--port', '8000']
