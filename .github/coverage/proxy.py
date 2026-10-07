import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class SonarProxy(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.forward()

    def do_POST(self) -> None:
        self.forward()

    def forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in {"host", "connection", "transfer-encoding"}
        }
        headers["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
        headers["CF-Access-Client-Secret"] = os.environ["CF_ACCESS_CLIENT_SECRET"]
        request = Request(
            os.environ["SONAR_HOST_URL"].rstrip("/") + self.path,
            data=body if self.command == "POST" else None,
            headers=headers,
            method=self.command,
        )
        try:
            response = build_opener(NoRedirect()).open(request, timeout=30)
        except HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                self.send_error(502, "Upstream redirects are refused")
                return
            response = error
        with response:
            payload = response.read()
            self.send_response(response.status)
            for name, value in response.headers.items():
                if name.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 9000), SonarProxy).serve_forever()
