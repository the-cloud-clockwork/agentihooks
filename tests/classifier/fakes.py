import io
import json
import urllib.error

KEY = "sk-test-classifier-secret-0042"

TIER_ANSWER = {
    "type": "choice",
    "choice": "small",
    "confidence": 0.9,
    "probabilities": {"small": 0.95, "large": 0.05},
}
EFFORT_ANSWER = {
    "type": "score",
    "score": 0.2,
    "confidence": 0.8,
    "legend": {"0": "low", "1": "high"},
    "probabilities": {"0": 0.8, "1": 0.2},
}
TRIVIAL_ANSWER = {"type": "noul", "noul": 0.74}


def payload(model="perplexity/pplx", cost=0.000012):
    return {
        "model": model,
        "answers": {"tier": TIER_ANSWER, "effort": EFFORT_ANSWER, "trivial": TRIVIAL_ANSWER},
        "usage": {"input_tokens": 300, "output_tokens": 3, "cost": cost},
    }


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok(body=None):
    return Response(json.dumps(body if body is not None else payload()).encode())


class HttpFailure(Exception):
    def __init__(self, code, body):
        super().__init__(code)
        self.code = code
        self.body = body

    def build(self):
        return urllib.error.HTTPError("http://x/v1/decisions", self.code, "err", {}, io.BytesIO(self.body))


def http_error(code, message="boom"):
    return HttpFailure(code, json.dumps({"error": {"message": message}}).encode())


class FakeUrlopen:
    def __init__(self, script):
        self.script = dict(script)
        self.calls = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data)
        self.calls.append(
            {
                "model": body["model"],
                "body": body,
                "headers": dict(request.header_items()),
                "timeout": timeout,
                "url": request.full_url,
            }
        )
        outcome = self.script[body["model"]]
        if isinstance(outcome, HttpFailure):
            raise outcome.build()
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(outcome.getvalue())
