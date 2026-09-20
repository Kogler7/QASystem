import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_extract_v3.client import JSONClient, ProviderConfig, ProviderError


class ClientTests(unittest.TestCase):
    def test_config_secret_not_in_repr_or_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'api.md'
            path.write_text('base_url="https://api.siliconflow.cn/v1"\nmodel = "deepseek-ai/DeepSeek-V4-Flash"\nAPIKEY: sk-test-secret\n')
            config = ProviderConfig.from_file(path)
        self.assertEqual(config.api_key, 'sk-test-secret')
        self.assertNotIn('sk-test-secret', repr(config))
        self.assertNotIn('sk-test-secret', json.dumps(config.safe_dict()))

    def test_json_mode_and_native_messages(self):
        client = JSONClient(ProviderConfig(api_key='test'))
        body = client.request_body([{'role': 'user', 'content': 'Return JSON.'}])
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertFalse(body['stream'])
        self.assertEqual(body['messages'][0]['role'], 'user')

    def test_incomplete_response_not_misreported_complete(self):
        response = JSONClient.decode_response({'choices': [{'message': {'content': '{'}, 'finish_reason': 'length'}]})
        self.assertEqual(response.status, 'incomplete')

    def test_error_is_redacted(self):
        import urllib.error
        client = JSONClient(ProviderConfig(api_key='sk-private'))
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('sk-private')):
            with self.assertRaises(ProviderError) as caught:
                client([{'role': 'user', 'content': 'json'}])
        self.assertNotIn('sk-private', str(caught.exception))

    def test_invalid_provider_response_finishes_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            client = JSONClient(ProviderConfig(api_key='sk-private'), trace_dir=directory)
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"choices":[]}')):
                with self.assertRaises(ProviderError):
                    client([{'role':'user','content':'Return JSON.'}])
            trace = json.loads((Path(directory)/'0001.json').read_text())
            self.assertEqual(trace['status'],'failed')
            self.assertEqual(trace['error_code'],'PROVIDER_NO_CHOICES')
            self.assertEqual(client.records[0]['status'],'failed')


if __name__ == '__main__':
    unittest.main()
