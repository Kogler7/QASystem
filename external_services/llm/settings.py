"""One declared configuration entry point for every scheduled LLM caller."""
from docker.configuration import declarations, getenv


def scheduler_settings(config: dict) -> dict:
    def read(key):
        return getenv(key, declarations()['items'][key]['default'])

    values = dict(config)
    for name, key, cast in (
        ('tokenizer_dir', 'KG_EXTRACT_LLM_TOKENIZER_DIR', str),
        ('tokenizer_revision', 'KG_EXTRACT_LLM_TOKENIZER_REVISION', str),
        ('max_input_bytes', 'KG_EXTRACT_LLM_MAX_INPUT_BYTES', int),
        ('quota_backend', 'KG_EXTRACT_LLM_QUOTA_BACKEND', str),
        ('adaptive_initial_input_tokens', 'KG_EXTRACT_LLM_ADAPTIVE_INITIAL_INPUT_TOKENS', int),
    ):
        if name not in values:
            values[name] = cast(read(key))
    if values['quota_backend'] not in {'local', 'redis'}:
        raise ValueError('Invalid extraction quota backend')
    if values['max_input_bytes'] < 1 or values['adaptive_initial_input_tokens'] < 0:
        raise ValueError('Invalid extraction scheduler limits')
    if values['quota_backend'] == 'redis' and not values.get('quota_redis_url'):
        values['quota_redis_url'] = (read('KG_EXTRACT_LLM_QUOTA_REDIS_URL')
                                   or read('EXECUTION_REDIS_URL'))
    return values
