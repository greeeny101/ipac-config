"""The stdlib HTTP adapter over `service.Service`.

The only module in the package that knows what a request is. Routes parse the
URL, hand plain data to the service, and turn what comes back into a response
- so the rules about what any of it *means* live in the service, not here.

The one exception is the SSE stream: it is a response that stays open for
minutes, so its loop cannot be anything but transport code.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import urllib.parse

from ..errors import DeviceError, ProtocolError, ReadOnlyError
from ..version import __version__
from .monitors import SSE_HEARTBEAT, sse_frame

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# An allowlist, not a directory listing: `serve` binds 0.0.0.0 by default, so
# the name in the URL is untrusted and must never reach os.path.join.
STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


def _make_handler(service):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "ipacconf/" + __version__

        def log_message(self, fmt, *fmt_args):
            sys.stderr.write("  %s\n" % (fmt % fmt_args))

        # -- helpers

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _asset(self, name, content_type):
            with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length))

        def _route(self):
            """The request path, unescaped, with the query string dropped."""
            return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        def _query(self):
            return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def _send_file(self, path, name):
            with open(path, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Content-Disposition", 'attachment; filename="%s"' % name
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, exc):
            if isinstance(exc, ReadOnlyError):
                return self._json({"error": str(exc)}, 403)
            if isinstance(exc, (DeviceError, ProtocolError)):
                return self._json({"error": str(exc)}, 500)
            # Surface anything else in the UI rather than in the log alone.
            return self._json(
                {"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        # -- live input

        def _flag(self, params, name):
            value = (params.get(name) or ["0"])[0]
            return value.lower() not in ("0", "", "false", "no")

        def _sse(self, payload, name=None):
            self.wfile.write(sse_frame(payload, name))
            self.wfile.flush()

        def _input_stream(self):
            """A long-lived text/event-stream of presses.

            The options ride in the query string because EventSource can only
            GET - which also makes an exclusive grab last exactly as long as
            the connection that asked for it.
            """
            params = self._query()
            grab, every = self._flag(params, "grab"), self._flag(params, "all")
            monitors = service.monitors
            try:
                monitor = monitors.acquire(grab, every)
            except (DeviceError, ProtocolError) as exc:
                return self._json({"error": str(exc)}, 503)

            events = monitor.stream.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self._sse(
                    {
                        "devices": [d.as_dict() for d in monitor.devices],
                        "grab": grab,
                        "all": every,
                        "matching": monitor.profile is not None,
                        "fake": bool(getattr(service.args, "fake_input", None)),
                    },
                    "watching",
                )
                while True:
                    try:
                        self._sse(events.get(timeout=SSE_HEARTBEAT))
                    except queue.Empty:
                        if monitor.error:
                            return self._sse({"error": monitor.error}, "fault")
                        # Also how a closed tab is noticed: this write fails.
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                monitor.stream.unsubscribe(events)
                monitors.release()

        # -- routes

        def do_GET(self):
            try:
                path = self._route()
                if path in ("/", "/index.html"):
                    return self._asset("index.html", "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    name = path[len("/static/"):]
                    if name not in STATIC_FILES:
                        return self._json({"error": "not found"}, 404)
                    return self._asset(name, STATIC_FILES[name])
                if path == "/api/saved":
                    return self._json(service.saved_list())
                if path.startswith("/api/saved/"):
                    ident = path[len("/api/saved/"):]
                    if ident.endswith("/download"):
                        full = service.saved_file(ident[: -len("/download")])
                        return self._send_file(full, os.path.basename(full))
                    return self._json(service.saved_get(ident))
                if path == "/api/codes":
                    return self._json(service.codes())
                if path == "/api/device":
                    return self._json(service.device())
                if path == "/api/config":
                    return self._json(service.config())
                if path == "/api/input/devices":
                    return self._json(service.input_devices())
                if path == "/api/input/stream":
                    return self._input_stream()
                if path == "/api/input":
                    since = int((self._query().get("since") or ["0"])[0])
                    return self._json(service.input_events(since))
                self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001 - surface it in the UI
                self._fail(exc)

        def do_POST(self):
            try:
                path = self._route()
                payload = self._body()
                if path == "/api/config":
                    return self._json(service.apply(payload))
                if path == "/api/restore":
                    return self._json(service.restore(payload))
                if path.startswith("/api/saved/") and path.endswith("/label"):
                    ident = path[len("/api/saved/"): -len("/label")]
                    return self._json(
                        service.saved_label(ident, payload.get("label")))
                if path == "/api/import":
                    return self._json(service.import_(payload))
                return self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

        def do_DELETE(self):
            try:
                path = self._route()
                if not path.startswith("/api/saved/"):
                    return self._json({"error": "not found"}, 404)
                ident = path[len("/api/saved/"):]
                return self._json(service.saved_delete(ident))
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

    return Handler
