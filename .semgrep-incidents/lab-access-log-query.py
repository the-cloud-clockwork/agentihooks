from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit


def raw_request_line(request: BaseHTTPRequestHandler) -> None:
    # ruleid: lab-access-log-query
    request.log_message('"%s" %s %s', request.requestline, 200, "-")


def raw_request_target(request: BaseHTTPRequestHandler) -> None:
    # ruleid: lab-access-log-query
    request.log_message('"GET %s HTTP/1.1" %s %s', request.path, 200, "-")


def query_free_request_target(request: BaseHTTPRequestHandler) -> None:
    # ok: lab-access-log-query
    request.log_message('"GET %s HTTP/1.1" %s %s', urlsplit(request.path).path, 200, "-")


def inherited_access_log(handle: Callable[[BaseHTTPRequestHandler], None]) -> type[BaseHTTPRequestHandler]:
    # ruleid: lab-handler-inherits-raw-access-log
    return type("Handler", (BaseHTTPRequestHandler,), {"do_GET": handle, "do_POST": handle})


def explicit_access_log(
    handle: Callable[[BaseHTTPRequestHandler], None], logger: Callable[[BaseHTTPRequestHandler, int, str], None]
) -> type[BaseHTTPRequestHandler]:
    # ok: lab-handler-inherits-raw-access-log
    return type("Handler", (BaseHTTPRequestHandler,), {"do_GET": handle, "do_POST": handle, "log_request": logger})
