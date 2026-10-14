"""One declared configuration entry point for every scheduled LLM caller."""
def scheduler_settings(config: dict) -> dict:
    values = dict(config)
    service = values.get('service_snapshot') or {}
    caps = service.get('capabilities') or {}
    for key in ('tokenizer_dir', 'tokenizer_revision', 'max_input_bytes'):
        if key in caps: values[key] = caps[key]
    for key in ('tokenizer_dir', 'tokenizer_revision'): values.setdefault(key, '')
    values.setdefault('max_input_bytes', None)
    values.setdefault('quota_backend', 'local')
    values.setdefault('adaptive_initial_input_tokens', 0)
    if values['quota_backend'] not in {'local', 'redis'}:
        raise ValueError('Invalid extraction quota backend')
    if (values['max_input_bytes'] is not None and values['max_input_bytes'] < 1) or values['adaptive_initial_input_tokens'] < 0:
        raise ValueError('Invalid extraction scheduler limits')
    if values['quota_backend'] == 'redis' and not values.get('quota_redis_url'):
        raise ValueError('Standalone shared quota requires an explicit test/development connection')
    return values
