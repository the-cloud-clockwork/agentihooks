import ast
import contextlib
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch


def main() -> None:
    script = (Path(__file__).resolve().parents[1] / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    embedded = script.split("python - <<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    tree = ast.parse(embedded)
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "send")
    namespace = {"json": json, "urllib": urllib, "sys": sys, "base": "http://controller", "token": "fixture"}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "kind-request", "exec"), namespace)
    send = namespace["send"]
    paths = [("POST", "/v2/executions/register"), ("PUT", "/v2/executions/probe/heartbeat")]
    reasons = ["controller lease lost", "heartbeat contention", "Redis unreachable", "backend refused"]
    for method, path in paths:
        for reason in reasons:
            body = json.dumps({"reason": reason})
            error = urllib.error.HTTPError(
                "http://controller" + path, 503, "unavailable", {}, io.BytesIO(body.encode())
            )
            output = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=error), contextlib.redirect_stderr(output):
                try:
                    send(path, {}, method)
                except urllib.error.HTTPError as caught:
                    assert caught is error, "the helper replaced the HTTP failure"
                else:
                    raise AssertionError("the helper swallowed the HTTP failure")
            assert f"{method} {path}: HTTP 503" in output.getvalue(), output.getvalue()
            assert body in output.getvalue(), output.getvalue()
            assert "fixture" not in output.getvalue(), "the helper printed authorization"
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b'{"renewal_sequence": 1}'
    with patch("urllib.request.urlopen", return_value=response) as opened:
        assert send(paths[1][1], {"renewal_sequence": 1}, "PUT") == (200, {"renewal_sequence": 1})
    request = opened.call_args.args[0]
    assert request.method == "PUT" and request.full_url == "http://controller" + paths[1][1]
    assert json.loads(request.data) == {"renewal_sequence": 1}
    assert opened.call_args.kwargs == {"timeout": 10}
    print("kind request proof passed: eight refusal bodies and one successful request")


if __name__ == "__main__":
    main()
