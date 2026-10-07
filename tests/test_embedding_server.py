import json
import os
import sys
from email.message import Message
from http.client import HTTPResponse
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from engram.embedding import SharedMLXEmbedder
from engram.embedding_server import start_embedding_server
from engram.errors import ModelUnavailableError


@pytest.fixture(autouse=True)
def sandbox_http_transport(monkeypatch):
    """Exercise the real handler without sockets only in the deny-network sandbox.

    Normal host tests retain the real loopback HTTP server and client transport.
    This adapter replaces transport alone, including urllib's HTTP error behavior.
    """
    if os.environ.get("ENGRAM_TEST_SANDBOX") != "1":
        return

    import engram.embedding
    import engram.embedding_server

    servers = {}

    class MemoryServer:
        def __init__(self, address, handler):
            assert address[0] == "127.0.0.1"
            assert len(servers) < 4, "bounded sandbox transport"
            self.server_address = (address[0], address[1] or 40000 + len(servers))
            self.handler = handler
            servers[self.server_address] = self

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            servers.pop(self.server_address, None)

    class InlineThread:
        def __init__(self, *, target, daemon, name):
            self.target = target

        def start(self):
            self.target()

    class MemorySocket:
        def __init__(self, data):
            self.data = data

        def makefile(self, mode):
            assert mode == "rb"
            return BytesIO(self.data)

    def memory_urlopen(request, timeout=None):
        url = urlsplit(request.full_url)
        assert url.scheme == "http" and url.hostname == "127.0.0.1"
        assert request.get_method() == "POST"
        server = servers[(url.hostname, url.port)]
        body = request.data or b""
        assert len(body) <= engram.embedding_server.MAX_BODY
        handler = server.handler.__new__(server.handler)
        handler.server = server
        handler.path = url.path + ("?" + url.query if url.query else "")
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"POST {handler.path} HTTP/1.1"
        handler.headers = Message()
        for key, value in request.header_items():
            handler.headers[key] = value
        if "Content-Length" not in handler.headers:
            handler.headers["Content-Length"] = str(len(body))
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.do_POST()
        response = HTTPResponse(MemorySocket(handler.wfile.getvalue()))
        response.begin()
        if response.status >= 400:
            raise HTTPError(request.full_url, response.status, response.reason, response.headers, response)
        return response

    monkeypatch.setattr(engram.embedding_server, "HTTPServer", MemoryServer)
    monkeypatch.setattr(engram.embedding_server, "Thread", InlineThread)
    monkeypatch.setattr(sys.modules[__name__], "urlopen", memory_urlopen)
    monkeypatch.setattr(engram.embedding, "urlopen", memory_urlopen)


class FakeEmbedder:
    model = "test-embed"

    def embed(self, texts):
        return [[float(len(text))] for text in texts]

    def embed_queries(self, texts):
        return [[float(len(text) + 100)] for text in texts]


def test_shared_server_distinguishes_queries_and_documents():
    server = start_embedding_server(FakeEmbedder(), port=0)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for kind, expected in (("document", 2.0), ("query", 102.0)):
            request = Request(
                base + "/embed",
                data=json.dumps({"texts": ["中文"], "kind": kind}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=2) as response:
                assert json.load(response)["vectors"] == [[expected]]
    finally:
        server.shutdown()
        server.server_close()


def test_shared_client_uses_one_running_server():
    server = start_embedding_server(FakeEmbedder(), port=0)
    try:
        class Local:
            model = "test-embed"
            dimensions = 1

            def embed(self, texts):
                raise AssertionError("local model should not load")

        client = SharedMLXEmbedder(
            Local(), base_url=f"http://127.0.0.1:{server.server_address[1]}"
        )
        assert client.embed_query("中文") == [102.0]
    finally:
        server.shutdown()
        server.server_close()


def test_shared_failure_does_not_load_second_model():
    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("model failed")

    class Local:
        model = "test-embed"
        dimensions = 1

        def embed(self, texts):
            raise AssertionError("local model should not load")

    server = start_embedding_server(Broken(), port=0)
    try:
        client = SharedMLXEmbedder(
            Local(), base_url=f"http://127.0.0.1:{server.server_address[1]}"
        )
        with pytest.raises(ModelUnavailableError, match="503"):
            client.embed(["中文"])
    finally:
        server.shutdown()
        server.server_close()
