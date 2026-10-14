"""Controlled, offline MinerU 4 runtime configuration; no deployment API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct

ONNX_FILES = (
    'Layout/PP-DocLayoutV2/inference.onnx', 'Layout/PP-DocLayoutV2/inference.yml',
    'OCR/paddleocr/ch_PP-OCRv6_tiny_det_infer.onnx',
    'OCR/paddleocr/ch_PP-OCRv6_tiny_det_inference.yml',
    'OCR/paddleocr/ch_PP-OCRv6_small_rec_infer.onnx',
    'OCR/paddleocr/ch_PP-OCRv6_small_rec_inference.yml',
    'OCR/paddleocr/seal_PP-OCRv4_det_infer.onnx',
    'OCR/paddleocr/seal_PP-OCRv4_det_inference.yml',
    'MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M.onnx',
    'MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M_inference.yml',
    'Table/slanet-plus.onnx', 'Table/unet.onnx', 'Table/PP-LCNet_x1_0_table_cls.onnx',
)
TORCH_FILES = (
    'Layout/PP-DocLayoutV2', 'OCR/paddleocr',
    'MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M.pth',
    'MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M_inference.yml',
    'Table/slanet-plus.onnx', 'Table/unet.onnx', 'Table/PP-LCNet_x1_0_table_cls.onnx',
)
GGUF_FILES = ('MinerU2.5-Pro-2605-1.2B-Q8_0.gguf', 'mmproj-MinerU2.5-Pro-2605-1.2B-Q8_0.gguf')


def absolute_path(value, name):
    if not isinstance(value, str) or not value or '${' in value or not Path(value).is_absolute():
        raise ValueError(f'{name} 必须显式配置为当前运行环境的绝对路径；请通过 ops 配置')
    return Path(value).resolve()


def settings():
    from qasystem_config.configuration import getenv, declarations, read_registry
    values = {
        'home': getenv('MINERU_HOME'),
        'base_dir': getenv('MINERU_MODEL_BASE_DIR'),
        'source': getenv('MINERU_MODEL_SOURCE'),
        'small_backend': getenv('MINERU_MODEL_SMALL_BACKEND'),
        'vlm_engine': getenv('MINERU_MODEL_VLM_ENGINE'),
    }
    values['declaration_version'] = declarations()['version']
    _, values['revision'] = read_registry(with_revision=True)
    return validate_settings(values)


def validate_settings(values):
    values = dict(values)
    values['home'] = str(absolute_path(values.get('home'), 'MINERU_HOME'))
    values['base_dir'] = str(absolute_path(values.get('base_dir'), 'MINERU_MODEL_BASE_DIR'))
    if values.get('source') != 'local':
        raise ValueError('MinerU 运行时模型来源必须是 local；下载请使用独立的模型准备工具')
    if values.get('small_backend') not in {'onnx', 'torch'}:
        raise ValueError('MinerU 小模型后端必须明确选择 onnx 或 torch')
    if values.get('vlm_engine') not in {'llama-cpp', 'vllm', 'lmdeploy', 'mlx'}:
        raise ValueError('MinerU VLM 引擎必须明确配置，不能自动回退')
    return values


def required_repositories(values, tier):
    values = validate_settings(values)
    if tier not in {'flash', 'basic', 'standard', 'advanced'}:
        raise ValueError('MinerU 档位无效')
    if tier == 'flash':
        return {}
    repos = {'MinerU-4_models_onnx': ONNX_FILES} if values['small_backend'] == 'onnx' else {
        'MinerU-4_models_torch': TORCH_FILES}
    if tier in {'standard', 'advanced'}:
        repos['MinerU2.5-Pro-2605-1.2B-GGUF' if values['vlm_engine'] == 'llama-cpp'
              else 'MinerU2.5-Pro-2605-1.2B'] = GGUF_FILES if values['vlm_engine'] == 'llama-cpp' else (
                  'config.json', 'tokenizer_config.json')
    return repos


def _safetensors_complete(path):
    with path.open('rb') as stream:
        raw = stream.read(8)
        if len(raw) != 8:
            return False
        size = struct.unpack('<Q', raw)[0]
        if not 2 <= size <= min(100_000_000, path.stat().st_size - 8):
            return False
        try:
            header = json.loads(stream.read(size))
            spans = sorted(item['data_offsets'] for key, item in header.items() if key != '__metadata__')
            cursor = 0
            for start, end in spans:
                if type(start) is not int or type(end) is not int or start != cursor or end < start:
                    return False
                cursor = end
            return bool(spans) and cursor > 0 and cursor == path.stat().st_size - 8 - size
        except (ValueError, KeyError, TypeError, AttributeError):
            return False


def model_report(values, tier, *, check_lock=False, require_receipt=True):
    values = validate_settings(values)
    base = Path(values['base_dir'])
    missing, invalid, checked = [], [], 0
    repositories = required_repositories(values, tier)
    for repo, names in repositories.items():
        folder = base / repo
        if not folder.resolve().is_relative_to(base):
            invalid.append(repo + ': 模型目录越出共同根目录')
            continue
        for name in names:
            path = folder / name
            if not path.resolve().is_relative_to(folder.resolve()):
                invalid.append(repo + '/' + name + ': 路径越界')
            elif path.is_dir():
                # The Torch provider's directory entries also need actual payload
                # and its own download receipt. A marker alone is never enough.
                payloads = [p for p in path.rglob('*') if p.is_file() and p.name != '.mineru_complete']
                weights = [p for p in payloads if p.suffix in {'.pth', '.bin', '.safetensors', '.onnx'}]
                if not weights or any(p.stat().st_size == 0 for p in weights):
                    missing.append(repo + '/' + name)
                elif require_receipt and not any((p / '.mineru_complete').is_file() for p in (path, folder)):
                    missing.append(repo + '/' + name + '/.mineru_complete')
                else:
                    checked += len(payloads)
            elif not path.is_file() or path.stat().st_size == 0:
                missing.append(repo + '/' + name)
            else:
                checked += 1
                if path.suffix == '.gguf':
                    with path.open('rb') as stream:
                        header = stream.read(24)
                    valid = len(header) == 24 and header[:4] == b'GGUF'
                    if valid:
                        version, tensors, metadata = struct.unpack('<IQQ', header[4:])
                        valid = version in (2, 3) and tensors > 0 and metadata > 0 and path.stat().st_size >= 24 + tensors * 32 + metadata * 13
                    if not valid:
                        invalid.append(repo + '/' + name + ': GGUF 文件头无效')
        if require_receipt and repo == 'MinerU2.5-Pro-2605-1.2B-GGUF' and not (folder / '.mineru_complete').is_file():
            missing.append(repo + '/.mineru_complete')
        if repo == 'MinerU2.5-Pro-2605-1.2B':
            index = folder / 'model.safetensors.index.json'
            try:
                if index.is_file():
                    weight_map = json.loads(index.read_text())['weight_map']
                    names = set(weight_map.values())
                    if not names:
                        raise ValueError()
                    weights = [folder / name for name in names]
                else:
                    weights = list(folder.glob('*.safetensors'))
                if not weights or any(not p.resolve().is_relative_to(folder.resolve()) or not p.is_file() or not _safetensors_complete(p) for p in weights):
                    invalid.append(repo + ': 权重分片缺失或截断')
                checked += len(weights)
            except (ValueError, KeyError, TypeError, OSError):
                invalid.append(repo + ': 权重索引无效')
            if require_receipt and not (folder / '.mineru_complete').is_file():
                missing.append(repo + '/.mineru_complete')
    if check_lock and repositories:
        import tempfile
        try:
            with tempfile.TemporaryFile(dir=base / '.locks'):
                pass
        except OSError:
            invalid.append('.locks: 模型锁目录不存在或不可写')
    return {'ok': not missing and not invalid, 'path': str(base), 'tier': tier,
            'checked_files': checked, 'missing': missing, 'invalid': invalid}


def prepare_runtime(values, tier, directory, inherited):
    values = validate_settings(values)
    report = model_report(values, tier, check_lock=True)
    if not report['ok']:
        raise ValueError('MinerU 本地模型未就绪：' + '; '.join(report['missing'] + report['invalid']))
    directory = Path(directory).resolve()
    configuration = {'model': {'base_dir': values['base_dir'], 'source': 'local',
        'small_backend': values['small_backend'], 'vlm': {'engine': values['vlm_engine'],
        'server_url': '', 'api_key': '', 'model': ''}},
        'llm_aided': {'features': {'title_leveling': False, 'cross_page_table_cell_merge': False}}}
    payload = json.dumps(configuration, ensure_ascii=False, sort_keys=True)
    config_path = directory / 'mineru-config.yaml'
    config_path.write_text(payload, encoding='utf-8')  # JSON is a YAML subset; no second parser or defaults.
    stamp = {'declaration_version': values.get('declaration_version'), 'revision': values.get('revision'),
             'fingerprint': hashlib.sha256(payload.encode()).hexdigest(), 'source': 'local',
             'legacy_config_ignored': True, 'checked_files': report['checked_files']}
    plan = {'config_path': str(config_path), 'stamp': stamp}
    plan_path = directory / 'mineru-plan.json'
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    # A child-only environment snapshot: never mutate the parent's environment.
    environment = {key: value for key, value in inherited.items() if not key.startswith('MINERU_')}
    environment.update({'MINERU_CONFIG': str(config_path), 'MINERU_HOME': values['home']})
    return plan_path, environment, stamp


def verify_loaded_configuration(plan_path):
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    config_path = Path(plan['config_path']).resolve()
    if config_path.parent != plan_path.parent:
        raise ValueError('MinerU 生成配置路径不属于本次解析')
    payload = config_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != plan['stamp']['fingerprint']:
        raise ValueError('MinerU 生成配置指纹不一致')
    from mineru.config import config, get_config_file_path
    if Path(get_config_file_path()).resolve() != config_path:
        raise ValueError('MinerU 未读取本次受控配置，拒绝回退到旧 YAML')
    expected = json.loads(payload)
    model = expected['model']
    if (config.model.source != 'local' or config.model.base_dir != model['base_dir'] or
        config.model.small_backend != model['small_backend'] or
        config.model.vlm.engine != model['vlm']['engine'] or config.model.vlm.server_url or
        config.llm_aided.features.title_leveling or config.llm_aided.features.cross_page_table_cell_merge):
        raise ValueError('MinerU 实际配置被覆盖，拒绝解析')
    return plan['stamp']
