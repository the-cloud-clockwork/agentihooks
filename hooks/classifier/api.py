from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hooks.classifier.errors import BackendFailure, ClassifierRequestError
from hooks.classifier.result import DecisionRequest, DecisionResult, parse_answers

KEY_VAR = "AGENTIHOOKS_CLASSIFIER_LITELLM_KEY"
ROUTE = "/v1/decisions"
LENGTH_ERROR = re.compile(r"context|token", re.IGNORECASE)


def _error_message(error: HTTPError) -> str:
    text = error.read().decode(errors="replace")
    try:
        return str(json.loads(text)["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return text


def _http_failure(model: str, error: HTTPError, key: str) -> Exception:
    if error.code in (401, 403):
        return BackendFailure(f"{model}: HTTP {error.code}, key refused", skip_api=True)
    if error.code != 400:
        return BackendFailure(f"{model}: HTTP {error.code}")
    message = _error_message(error)
    if LENGTH_ERROR.search(message):
        return BackendFailure(f"{model}: HTTP 400, input too long")
    if key:
        message = message.replace(key, "[redacted]")
    return ClassifierRequestError(f"{model}: HTTP 400: {message[:500]}")


class DecisionsApiBackend:
    def __init__(self, model: str, url: str, timeout_s: float):
        self.name = model
        self.url = url.rstrip("/") + ROUTE
        self.timeout_s = timeout_s

    def decide(self, request: DecisionRequest) -> DecisionResult:
        key = os.environ.get(KEY_VAR, "")
        body = json.dumps({"model": self.name, **request.wire()}).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        try:
            with urlopen(Request(self.url, data=body, headers=headers), timeout=self.timeout_s) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            raise _http_failure(self.name, error, key) from None
        except TimeoutError:
            raise BackendFailure(f"{self.name}: timeout") from None
        except (URLError, OSError) as error:
            reason = "timeout" if isinstance(getattr(error, "reason", None), TimeoutError) else "connection error"
            raise BackendFailure(f"{self.name}: {reason}") from None
        except ValueError:
            raise BackendFailure(f"{self.name}: parse error, response is not JSON") from None
        try:
            return DecisionResult(
                answers=parse_answers(payload, request.questions, self.name),
                source=self.name,
                cost=(payload.get("usage") or {}).get("cost"),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BackendFailure(f"{self.name}: parse error, invalid answers") from None
