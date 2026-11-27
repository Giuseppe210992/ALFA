"""Finto server OpenAI-compatibile per i test (nessuna rete, nessuna chiave reale).

Il comportamento dipende dalla chiave usata nell'header Authorization:
  good        -> 200, risponde "OK: <prompt>"
  bad         -> 401
  rate        -> 429 con Retry-After: 1 le prime `rate_n` volte, poi 200
  daily       -> 429 "Rate limit exceeded: free-models-per-day"
  minute      -> 429 "Rate limit reached for requests per minute" (senza Retry-After)
  empty       -> 200 con content vuoto
  think       -> 200 con <think>...</think> davanti alla risposta
  err500      -> 500
  slow        -> attende `slow_s` secondi poi 200
  nomodel     -> 404 model not found
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenAI:
    def __init__(self, rate_n=1, slow_s=2.0):
        self.calls = []
        self.rate_n = rate_n
        self.plan_json = "{}"
        self.slow_s = slow_s
        self._rate_seen = 0
        self._lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    return self._send(200, {"data": [{"id": "fake-model"}]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                key = (self.headers.get("Authorization") or "")
                key = (key[7:] if key.startswith("Bearer ") else key).strip()
                with fake._lock:
                    fake.calls.append({"key": key, "body": body, "path": self.path, "t": time.time()})
                if not self.path.endswith("/chat/completions"):
                    return self._send(404, {"error": "not found"})
                prompt = body.get("messages", [{}])[-1].get("content", "")
                ok = {"choices": [{"message": {"role": "assistant", "content": f"OK: {prompt}"}}],
                      "model": body.get("model")}
                if key == "plan":      # risponde con il piano JSON impostato dal test (analisi di un repository)
                    ok["choices"][0]["message"]["content"] = fake.plan_json
                    return self._send(200, ok)
                if key == "good":
                    return self._send(200, ok)
                if key == "bad":
                    return self._send(401, {"error": {"message": "Invalid API key"}})
                if key == "rate":
                    with fake._lock:
                        fake._rate_seen += 1
                        limited = fake._rate_seen <= fake.rate_n
                    if limited:
                        return self._send(429, {"error": {"message": "slow down"}}, {"Retry-After": "1"})
                    return self._send(200, ok)
                if key == "daily":
                    return self._send(429, {"error": {"message": "Rate limit exceeded: free-models-per-day"}})
                if key == "minute":
                    return self._send(429, {"error": {"message": "Rate limit reached for requests per minute"}})
                if key == "empty":
                    return self._send(200, {"choices": [{"message": {"content": ""}}]})
                if key == "think":
                    ok["choices"][0]["message"]["content"] = "<think>ragiono...</think>\nRisposta finale"
                    return self._send(200, ok)
                if key == "err500":
                    return self._send(500, {"error": "boom"})
                if key == "slow":
                    time.sleep(fake.slow_s)
                    return self._send(200, ok)
                if key == "nomodel":
                    return self._send(404, {"error": {"message": "model not found"}})
                return self._send(401, {"error": {"message": "unknown key"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    import sys
    f = FakeOpenAI()
    if os.environ.get("FAKE_PLAN_JSON"):
        f.plan_json = open(os.environ["FAKE_PLAN_JSON"], encoding="utf-8").read()
    if len(sys.argv) > 1:
        f.server.server_close()
        f.server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), f.server.RequestHandlerClass)
        f.port = int(sys.argv[1])
    print(f"fake OpenAI su http://127.0.0.1:{f.port}/v1", flush=True)
    f.server.serve_forever()
