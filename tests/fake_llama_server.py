"""A stand-in for llama.cpp's llama-server, for tests: same flags, /health (503 while "loading", then 200) and a
streaming OpenAI-style /v1/chat/completions. Replies come from `<model>.script.json` (a list of strings, used in
order; the last one repeats). Every request, the argv and the environment's keys are appended to `<model>.log.jsonl`.
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("-m", "--model", required=True)
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8080)
parser.add_argument("-c", "--ctx-size")
parser.add_argument("-ngl", "--n-gpu-layers")
parser.add_argument("-np", "--parallel")
parser.add_argument("-t", "--threads")
parser.add_argument("--offline", action="store_true")
args = parser.parse_args()

SCRIPT = json.load(open(args.model + ".script.json", encoding="utf-8"))
LOG = args.model + ".log.jsonl"
STATE = {"health_calls": 0, "reply": 0}
if os.path.exists(args.model + ".crash"):
    sys.exit(3)


def log(entry: dict) -> None:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


log({"argv": sys.argv[1:], "env": sorted(os.environ)})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        STATE["health_calls"] += 1
        ready = STATE["health_calls"] > 2
        body = b'{"status": "ok"}' if ready else b'{"error": {"code": 503, "message": "Loading model"}}'
        self.send_response(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        log({"request": body})
        reply = SCRIPT[min(STATE["reply"], len(SCRIPT) - 1)]
        STATE["reply"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(0, len(reply), 4):
            chunk = {"object": "chat.completion.chunk", "choices": [{"delta": {"content": reply[i:i + 4]}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
