"""Loopback-only CFuse transport for the existing HTTP evaluator.

Reads the user's CodeFuse login at startup; credentials remain in memory.
The gateway wraps non-streaming chat completions in SSE, so unwrap exactly one
complete response. This does not run extraction, alter prompts, or score results.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time

import httpx

MODEL = "deepseek-v4-flash-0731"
TOKEN_URL = "https://codexmuse.antgroup-inc.cn/v1/container/user/tokens/caselike"
CHAT_URL = "https://caselike.alipay.com/v1/cli/chat/completions"


class UpstreamCompletionError(ValueError):
    """Only fixed categories and bounded retry hints may leave the proxy."""
    def __init__(self, status=502, category='upstream_completion_error', retry_after=10):
        super().__init__(category)
        self.status, self.category, self.retry_after = status, category, retry_after


def _upstream_error(result):
    if not isinstance(result, dict):
        return None
    error = result.get('error')
    # CodeFuse also returns HTTP 200 + SSE with a top-level OpenAI error
    # envelope (object=error, code=429), without an error wrapper or success.
    if not error and result.get('success') is not False and result.get('object') != 'error':
        return None
    data = error if isinstance(error, dict) else result
    # COSEC: Inspect provider text solely to classify it; never echo/persist it.
    hint = ' '.join(str(data.get(k, ''))[:256] for k in ('code', 'type', 'message')).casefold()
    code = str(data.get('status') or data.get('status_code') or data.get('code') or '')
    if code == '429' or any(word in hint for word in ('rate_limit', 'rate limit', 'too many requests', '限流', '请求过多')):
        status, category = 429, 'upstream_rate_limit'
    elif code in {'401', '403'} or any(word in hint for word in ('unauthorized', 'invalid_api_key', 'token_expired', 'authentication', '鉴权失败')):
        status, category = 401, 'upstream_authentication_error'
    else:
        status, category = 502, 'upstream_completion_error'
    retry_after = 10
    try:
        value = float(data.get('retry_after', 10))
        if 0 < value < float('inf'):
            retry_after = int(min(60, max(1, value)))
    except (ValueError, TypeError):
        pass
    return UpstreamCompletionError(status, category, retry_after if status != 401 else 0)


def decode_completion(body: str) -> dict:
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        records = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        records = [line for line in records if line and line != "[DONE]"]
        if len(records) != 1:
            raise ValueError("Expected one complete non-streaming response") from None
        result = json.loads(records[0])
    upstream_error = _upstream_error(result)
    if upstream_error is not None:
        raise upstream_error
    if not isinstance(result, dict) or not result.get("choices"):
        raise UpstreamCompletionError(category='upstream_missing_completion')
    if result.get("model") != MODEL:
        raise ValueError("Gateway returned a different model")
    if any(not isinstance(c.get("message"), dict) or not c.get("finish_reason")
           for c in result["choices"]):
        raise ValueError("Incomplete chat response")
    return result


def load_headers(client: httpx.Client) -> dict:
    # COSEC: Use only the authorized client's fixed config paths. Do not log or
    # persist login contents or derived transport headers in evaluation artifacts.
    root = Path.home() / ".codefuse/fuse"
    login = json.loads((root / "codefuse.json").read_text())
    cache = json.loads((root / ".auth.json").read_text())
    response = client.get(TOKEN_URL, params={"productType": cache["productType"]},
        headers={"X-AGENT-USER": login["workid"], "X-AGENT-TOKEN": login["token"]})
    if response.status_code != 200:
        raise ValueError("CodeFuse credential exchange failed")
    value = response.json()
    if not value.get("success") or not value.get("data", {}).get("token"):
        raise ValueError("CodeFuse credential exchange failed")
    auth = value["data"]
    return {"Content-Type": "application/json", "X-USER": auth.get("workNo") or login["workid"],
        "userToken": auth["token"], "X-CALLER-PRODUCT": auth.get("productType") or cache["productType"],
        "X-MODEL-PROVIDER": "ANT_CHAT", "X-CHAT-DATA-FORMAT": "openai"}


class RequestPacer:
    """Share a conservative request cadence across extraction and judge calls."""
    def __init__(self, requests_per_minute=0, *, clock=time.monotonic, sleep=time.sleep):
        if type(requests_per_minute) is not int or requests_per_minute < 0:
            raise ValueError('requests_per_minute must be a nonnegative integer')
        self.interval = 60 / requests_per_minute if requests_per_minute else 0
        self.clock, self.sleep = clock, sleep
        self.next_request = 0.0
        self.lock = threading.Lock()

    def wait(self):
        if not self.interval:
            return
        with self.lock:
            delay = self.next_request - self.clock()
            if delay > 0:
                self.sleep(delay)
            self.next_request = self.clock() + self.interval


def make_handler(client, headers, *, requests_per_minute=0):
    slots = threading.BoundedSemaphore(4)
    pacer = RequestPacer(requests_per_minute)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, status, payload, retry_after=0):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if retry_after:
                self.send_header('Retry-After', str(retry_after))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.send_json(200 if self.path == "/health" else 404,
                {"status": "ok", "model": MODEL} if self.path == "/health" else {"error": "not found"})

        def do_POST(self):
            # COSEC: A fixed route/upstream/model, no forwarded caller headers,
            # no redirects, and no browser origins. Never expose credentials in errors.
            if (self.path != "/v1/chat/completions" or self.headers.get("Origin")
                    or self.headers.get_content_type() != "application/json"):
                self.send_json(403, {"error": {"message": "Unsupported request"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if payload.get("model") != MODEL or payload.get("stream", False):
                    raise ValueError("Only the configured model and non-streaming requests are supported")
                # Both evaluation roles use the same non-thinking configuration.
                payload.setdefault("thinking", {"type": "disabled"})
                with slots:
                    pacer.wait()
                    response = client.post(CHAT_URL, headers=headers, json=payload)
                if response.status_code != 200:
                    self.send_json(response.status_code, {"error": {
                        "message": f"CFuse upstream HTTP {response.status_code}"}})
                    return
                try:
                    result = decode_completion(response.text)
                except UpstreamCompletionError as exc:
                    print(json.dumps({'event': 'cfuse_upstream_error', 'category': exc.category,
                        'status': exc.status, 'retry_after': exc.retry_after}), flush=True)
                    self.send_json(exc.status, {'error': {'message': exc.category}}, exc.retry_after)
                    return
                except ValueError as exc:
                    # COSEC: Fixed decoder reasons and sizes only; never upstream
                    # content, login headers, tokens or model input in diagnostics.
                    allowed = {'Expected one complete non-streaming response',
                               'Gateway returned no chat completion',
                               'Gateway returned a different model', 'Incomplete chat response'}
                    reason = str(exc) if str(exc) in allowed else 'Invalid completion'
                    print(json.dumps({'event': 'cfuse_decode_failed', 'reason': reason,
                        'response_bytes': len(response.content),
                        'sse_records': sum(line.startswith('data:') for line in response.text.splitlines())}), flush=True)
                    raise
                self.send_json(200, result)
            except Exception as exc:
                self.send_json(502, {"error": {"message": f"CFuse transport failed: {type(exc).__name__}"}})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18089)
    parser.add_argument("--requests-per-minute", type=int, default=0,
                        help="Shared extraction/judge request rate; 0 disables pacing")
    args = parser.parse_args()
    if args.requests_per_minute < 0:
        parser.error('--requests-per-minute must be nonnegative')
    # COSEC: Fixed internal HTTPS services require intranet access. Arbitrary
    # caller URLs are never accepted; TLS verification stays on and redirects off.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=300) as client:
        headers = load_headers(client)
        server = ThreadingHTTPServer(("127.0.0.1", args.port),
            make_handler(client, headers, requests_per_minute=args.requests_per_minute))
        print(f"CFuse evaluation transport ready: 127.0.0.1:{args.port}, model={MODEL}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
