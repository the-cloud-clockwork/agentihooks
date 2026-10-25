import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = "code-mode-host-42"
PROGRAM = 'const r = await tools.exec_command({cmd: "echo code-mode-host-$((6*7))"});\ntext(r);'
USAGE = {
    "input_tokens": 1,
    "output_tokens": 1,
    "total_tokens": 2,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0},
}


class Model(BaseHTTPRequestHandler):
    outputs: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        returned = [i for i in body.get("input", []) if i.get("type") == "custom_tool_call_output"]
        Model.outputs.extend(returned)
        if returned:
            item = {
                "type": "message",
                "role": "assistant",
                "id": "msg_1",
                "status": "completed",
                "content": [{"type": "output_text", "text": "done", "annotations": []}],
            }
        else:
            item = {
                "type": "custom_tool_call",
                "id": "ctc_1",
                "call_id": "call_1",
                "status": "completed",
                "name": "exec",
                "input": PROGRAM,
            }
        response = {"id": "resp_1", "object": "response", "status": "completed", "output": [item], "usage": USAGE}
        events = [
            {"type": "response.created", "response": {**response, "output": []}},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write("".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode())

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def code_mode_model(codex: str, env: dict) -> str:
    catalog = json.loads(
        subprocess.run([codex, "debug", "models"], check=True, capture_output=True, text=True, env=env).stdout
    )
    model = next((m["slug"] for m in catalog["models"] if m.get("tool_mode") == "code_mode_only"), None)
    if model is None:
        sys.exit("Codex model catalog offers no code mode model")
    return model


def main() -> None:
    codex = sys.argv[1] if len(sys.argv) > 1 else "codex"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with server, tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "CODEX_HOME": home, "CODEX_PROBE_KEY": "probe"}
        model = code_mode_model(codex, env)
        provider = {
            "model_provider": '"probe"',
            "model_providers.probe.name": '"probe"',
            "model_providers.probe.base_url": f'"http://127.0.0.1:{server.server_port}/v1"',
            "model_providers.probe.wire_api": '"responses"',
            "model_providers.probe.env_key": '"CODEX_PROBE_KEY"',
        }
        command = [
            codex,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "-s",
            "danger-full-access",
            "-m",
            model,
        ]
        for key, value in provider.items():
            command += ["-c", f"{key}={value}"]
        try:
            result = subprocess.run(
                [*command, "run the probe"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=90,
            )
        finally:
            server.shutdown()
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    errors = [
        e["item"]["message"]
        for e in events
        if e.get("item", {}).get("type") == "error" and "Code Mode" in e["item"]["message"]
    ]
    returned = "".join(json.dumps(o.get("output")) for o in Model.outputs)
    report = {"model": model, "exit": result.returncode, "code_mode_errors": errors, "host_output": MARKER in returned}
    print(json.dumps(report))
    if result.returncode or errors or not report["host_output"]:
        sys.exit(f"Codex shell command through the code mode host failed: {result.stderr[-2000:]}")


if __name__ == "__main__":
    main()
