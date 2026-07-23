"""Custom RPC server for the Bingo Rush Canopy plugin.

Exposes read-only, chain-specific endpoints backed by the detached
``Plugin.query_state`` path (see contract/plugin.py). The plugin owns this HTTP
server entirely; Canopy core is unaware of these routes.

Routes:
  GET /v1/query/gems?address=<hex>       -> {"address", "amount"}
  GET /v1/query/cosmetic?token_id=<hex>  -> {"tokenID", "kind", "owner"} | 404
"""

import asyncio
import json
import logging
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

from .plugin import Plugin, PLUGIN_BUILD
from .proto import PluginStateReadRequest, PluginKeyRead

logger = logging.getLogger(__name__)


class PluginRPCHandler(BaseHTTPRequestHandler):
    """HTTP handler for the plugin's custom endpoints. Plugin injected by start_rpc_server()."""

    plugin: Optional[Plugin] = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/v1/query/gems":
                self._handle_gems(query)
            elif parsed.path == "/v1/query/cosmetic":
                self._handle_cosmetic(query)
            else:
                self._write_json_error(404, "not found")
        except Exception as exc:  # never crash the server thread
            logger.warning("plugin RPC handler error: %s", exc)
            self._write_json_error(500, str(exc))

    # -- state access ---------------------------------------------------------

    def _read_value(self, key: bytes) -> Optional[bytes]:
        """Single-key detached read at latest committed height."""
        coro = self.plugin.query_state(
            0,
            PluginStateReadRequest(keys=[PluginKeyRead(query_id=random.getrandbits(62), key=key)]),
        )
        future = asyncio.run_coroutine_threadsafe(coro, self.plugin._loop)
        resp = future.result(timeout=15.0)
        for r in resp.results:
            if r.entries and r.entries[0].value:
                return r.entries[0].value
        return None

    # -- handlers -------------------------------------------------------------

    def _handle_gems(self, query: dict) -> None:
        from .contract import key_for_gems, unmarshal
        from .proto.tx_pb2 import GemBalance
        addr = (query.get("address") or [None])[0]
        if not addr:
            return self._write_json_error(400, "address query param required")
        value = self._read_value(key_for_gems(bytes.fromhex(addr)))
        amount = 0
        if value:
            amount = unmarshal(GemBalance, value).amount
        self._write_json({"address": addr, "amount": amount})

    def _handle_cosmetic(self, query: dict) -> None:
        from .contract import key_for_cosmetic, unmarshal
        from .proto.tx_pb2 import Cosmetic
        token_id = (query.get("token_id") or [None])[0]
        if not token_id:
            return self._write_json_error(400, "token_id query param required")
        value = self._read_value(key_for_cosmetic(bytes.fromhex(token_id)))
        if not value:
            return self._write_json_error(404, "not found")
        cos = unmarshal(Cosmetic, value)
        self._write_json({
            "tokenID": token_id,
            "kind": cos.kind,
            "owner": cos.owner_address.hex(),
        })

    # -- io helpers -----------------------------------------------------------

    def _write_json(self, body: dict, status: int = 200) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json_error(self, status: int, message: str) -> None:
        self._write_json({"error": message}, status)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("plugin RPC: %s", format % args)


def start_rpc_server(plugin: Plugin) -> Optional[ThreadingHTTPServer]:
    """Launch the plugin's HTTP server with the Bingo Rush routes."""
    addr = plugin.config.rpc_address
    if not addr:
        logger.info("plugin RPC server disabled (no rpc_address configured)")
        return None
    try:
        host, _, port_str = addr.rpartition(":")
        if not host:
            host = "0.0.0.0"
        port = int(port_str)
        handler_cls = type("BoundPluginRPCHandler", (PluginRPCHandler,), {"plugin": plugin})
        server = ThreadingHTTPServer((host, port), handler_cls)
    except (OSError, ValueError) as exc:
        logger.warning(f"plugin RPC server disabled (failed to start on {addr!r}): {exc}")
        return None

    logger.info(f"plugin RPC server ({PLUGIN_BUILD}) listening on {addr}")
    logger.info("plugin RPC routes: /v1/query/gems, /v1/query/cosmetic")
    thread = threading.Thread(target=server.serve_forever, name="plugin-rpc", daemon=True)
    thread.start()
    return server
