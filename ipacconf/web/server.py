"""Starting the web UI: one threaded server over one Service."""

from __future__ import annotations

from ..version import __version__
from .handler import _make_handler
from .service import Service


def serve(args) -> int:
    import socketserver

    handler = _make_handler(Service(args))

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((args.host, args.port), handler) as httpd:
        where = args.host if args.host != "0.0.0.0" else _lan_address()
        print("ipacconf %s" % __version__)
        print("open http://%s:%d/" % (where, args.port))
        if getattr(args, "fake_device", None):
            print("(fake device: %s)" % args.fake_device)
        if getattr(args, "fake_input", None):
            print("(fake input: %s)" % args.fake_input)
        print("ctrl-c to stop")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


def _lan_address() -> str:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packets are sent
        return sock.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        sock.close()
