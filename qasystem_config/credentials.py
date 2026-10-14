"""Exact credential consumers; variable spelling never grants read access."""
import re

FIXED_CREDENTIALS = frozenset({'GRAPH_LLM_API_KEY', 'KG_EXTRACT_LLM_API_KEY',
    'SILICONFLOW_API_KEY', 'DEEPSEEK_API_KEY', 'AGENT_LLM_API_KEY',
    'EMBEDDING_SERVICE_API_KEY', 'CLEF_SERVICE_API_KEY', 'CLEF_API_KEY',
    'NANOJEV_SERVICE_API_KEY', 'NANOJEV_API_KEY', 'DOCUMENT_PARSE_SERVICE_API_KEY'})
CREDENTIAL_CONTRACT = {'fixed_credentials': sorted(FIXED_CREDENTIALS),
    'dynamic_consumers': ['services.*.api_key_env', 'models.*.engine_options.api_key_env'],
    'read_authority': 'verified-registry-revision', 'default': '',
    'storage': 'private-deployment-env', 'json_history': 'fingerprint-only'}


def bindings(document):
    from qasystem_config.configuration import declarations
    catalog = declarations()
    found = {}
    def add(key, consumer):
        if not key:
            return
        if not isinstance(key, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError('服务凭据引用名无效')
        if key in catalog.get('retired', ()) or (key in catalog['items'] and key not in FIXED_CREDENTIALS):
            raise ValueError('服务凭据不能引用退休或非凭据配置')
        found.setdefault(key, []).append(consumer)
    for name, service in document.get('services', {}).items():
        add(service.get('api_key_env'), 'services.' + name + '.api_key_env')
    for name, model in document.get('models', {}).items():
        add(model.get('engine_options', {}).get('api_key_env'), 'models.' + name + '.engine_options.api_key_env')
    return {key: tuple(consumers) for key, consumers in found.items()}


def fields(document):
    from qasystem_config.configuration import declarations
    return [dict(key=key, default='', secret=True, hot=False, process=False,
                 group='已登记服务凭据', dynamic=True,
                 description='服务凭据，默认空；私有环境文件保存，JSON历史只记录指纹。消费者：' + '、'.join(consumers))
            for key, consumers in sorted(bindings(document).items())
            if key not in declarations()['items']]
