"""Loopback-only shared MLX embedding endpoint inside the existing Engram process."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from engram.embedding import MLXEmbedder

MAX_BODY = 256_000
MAX_ITEMS = 32


def start_embedding_server(embedder: MLXEmbedder, *, port: int = 8772):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return
            self._json(200, {"ok": True, "model": embedder.model})

        def do_POST(self):
            if self.path not in ("/embed", "/v1/embeddings"):
                self._json(404, {"error": "not found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("request body size is out of range")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise TypeError("request must be an object")
                texts = payload.get("texts") if self.path == "/embed" else payload.get("input")
                if isinstance(texts, str):
                    texts = [texts]
                if (
                    not isinstance(texts, list)
                    or not 1 <= len(texts) <= MAX_ITEMS
                    or any(not isinstance(x, str) or len(x) > 12_000 for x in texts)
                ):
                    raise ValueError("texts must be 1-32 strings of at most 12000 characters")
                kind = payload.get("kind", payload.get("input_type", "document"))
                if kind not in ("document", "query"):
                    raise ValueError("kind must be document or query")
                vectors = (
                    embedder.embed_queries(texts) if kind == "query" else embedder.embed(texts)
                )
                if self.path == "/embed":
                    self._json(200, {"model": embedder.model, "vectors": vectors})
                else:
                    self._json(
                        200,
                        {
                            "object": "list",
                            "model": embedder.model,
                            "data": [
                                {"object": "embedding", "index": i, "embedding": vector}
                                for i, vector in enumerate(vectors)
                            ],
                        },
                    )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - HTTP boundary must survive bad model calls
                self._json(503, {"error": type(exc).__name__})

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True, name="engram-mlx-embed")
    thread.start()
    return server
