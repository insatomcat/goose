"""Service GOOSE : envoi continu de flux GOOSE avec API HTTP."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .transport import _build_frame
from .types import GoosePDU


@dataclass
class GooseStream:
    """Configuration d'un flux GOOSE envoyé en continu."""

    id: str
    iface: str
    src_mac: str
    dst_mac: str
    app_id: int
    vlan_id: Optional[int]
    vlan_priority: Optional[int]
    gocb_ref: str
    dat_set: str
    go_id: str
    ttl: int
    conf_rev: int
    simulation: bool
    nds_com: bool
    all_data: List[Any]

    st_num: int = 1
    sq_num: int = 0
    next_send_time: float = field(default_factory=time.monotonic)
    current_interval_ms: float = 10.0

    def to_pdu(self) -> GoosePDU:
        return GoosePDU(
            gocb_ref=self.gocb_ref,
            time_allowed_to_live=self.ttl,
            dat_set=self.dat_set,
            go_id=self.go_id,
            timestamp=datetime.now(timezone.utc),
            st_num=self.st_num,
            sq_num=self.sq_num,
            simulation=self.simulation,
            conf_rev=self.conf_rev,
            nds_com=self.nds_com,
            num_dat_set_entries=len(self.all_data),
            all_data=list(self.all_data),
        )


def _stream_to_dict(s: GooseStream) -> Dict[str, Any]:
    return {
        "id": s.id,
        "iface": s.iface,
        "src_mac": s.src_mac,
        "dst_mac": s.dst_mac,
        "app_id": s.app_id,
        "vlan_id": s.vlan_id,
        "vlan_priority": s.vlan_priority,
        "gocb_ref": s.gocb_ref,
        "dat_set": s.dat_set,
        "go_id": s.go_id,
        "ttl": s.ttl,
        "conf_rev": s.conf_rev,
        "simulation": s.simulation,
        "nds_com": s.nds_com,
        "all_data": s.all_data,
        "st_num": s.st_num,
        "sq_num": s.sq_num,
    }


def _parse_all_data(raw: List[Any]) -> List[Any]:
    """Convertit les valeurs JSON (y compris tuples raw) en all_data Python."""
    result: List[Any] = []
    for item in raw:
        if isinstance(item, list) and len(item) == 3 and item[0] == "raw":
            result.append(("raw", int(item[1]), str(item[2])))
        else:
            result.append(item)
    return result


class GooseService:
    """Service d'envoi continu de flux GOOSE, configurable via API HTTP."""

    IEC_MIN_MS = 10
    IEC_MAX_MS = 2000

    def __init__(self, host: str = "localhost", port: int = 7053) -> None:
        self.host = host
        self.port = port
        self._streams: Dict[str, GooseStream] = {}
        self._streams_lock = threading.Lock()
        self._stop = threading.Event()
        self._sender_thread: Optional[threading.Thread] = None
        self._http_server: Optional[HTTPServer] = None

    def add_stream(self, config: Dict[str, Any]) -> GooseStream:
        with self._streams_lock:
            stream_id = str(uuid.uuid4())
            all_data = _parse_all_data(config.get("all_data", []))
            s = GooseStream(
                id=stream_id,
                iface=config["iface"],
                src_mac=config["src_mac"],
                dst_mac=config["dst_mac"],
                app_id=config["app_id"],
                vlan_id=config.get("vlan_id"),
                vlan_priority=config.get("vlan_priority"),
                gocb_ref=config["gocb_ref"],
                dat_set=config["dat_set"],
                go_id=config["go_id"],
                ttl=config.get("ttl", 5000),
                conf_rev=config.get("conf_rev", 1),
                simulation=config.get("simulation", False),
                nds_com=config.get("nds_com", False),
                all_data=all_data,
                st_num=1,
                sq_num=0,
                next_send_time=time.monotonic(),
                current_interval_ms=float(max(self.IEC_MIN_MS, 1)),
            )
            self._streams[stream_id] = s
            return s

    def modify_stream(self, stream_id: str, updates: Dict[str, Any]) -> Optional[GooseStream]:
        with self._streams_lock:
            s = self._streams.get(stream_id)
            if s is None:
                return None
            if "all_data" in updates:
                s.all_data = _parse_all_data(updates["all_data"])
            if "ttl" in updates:
                s.ttl = updates["ttl"]
            if "gocb_ref" in updates:
                s.gocb_ref = updates["gocb_ref"]
            if "dat_set" in updates:
                s.dat_set = updates["dat_set"]
            if "go_id" in updates:
                s.go_id = updates["go_id"]
            if "conf_rev" in updates:
                s.conf_rev = updates["conf_rev"]
            if "simulation" in updates:
                s.simulation = updates["simulation"]
            if "nds_com" in updates:
                s.nds_com = updates["nds_com"]
            s.st_num += 1
            s.next_send_time = time.monotonic()
            s.current_interval_ms = float(max(self.IEC_MIN_MS, 1))
            return s

    def delete_stream(self, stream_id: str) -> bool:
        with self._streams_lock:
            return self._streams.pop(stream_id, None) is not None

    def get_stream(self, stream_id: str) -> Optional[GooseStream]:
        with self._streams_lock:
            return self._streams.get(stream_id)

    def list_streams(self) -> List[GooseStream]:
        with self._streams_lock:
            return list(self._streams.values())

    def _sender_loop(self) -> None:
        while not self._stop.wait(0.01):
            now = time.monotonic()
            due: List[GooseStream] = []
            with self._streams_lock:
                for s in self._streams.values():
                    if s.next_send_time <= now:
                        due.append(s)
            for s in due:
                try:
                    self._send_one(s)
                except Exception:
                    pass
                with self._streams_lock:
                    ss = self._streams.get(s.id)
                    if ss is not None:
                        ss.sq_num += 1
                        interval_s = ss.current_interval_ms / 1000.0
                        ss.next_send_time = time.monotonic() + interval_s
                        ss.current_interval_ms = min(
                            ss.current_interval_ms * 2.0,
                            float(self.IEC_MAX_MS),
                        )

    def _send_one(self, s: GooseStream) -> None:
        pdu = s.to_pdu()
        raw = _build_frame(
            dst_mac=s.dst_mac,
            src_mac=s.src_mac,
            app_id=s.app_id,
            pdu=pdu,
            vlan_id=s.vlan_id,
            vlan_priority=s.vlan_priority,
        )
        from scapy.all import sendp  # type: ignore[import-untyped]

        sendp(raw, iface=s.iface, count=1, inter=0.0, verbose=False)

    def start(self) -> None:
        self._stop.clear()
        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()

        handler = make_handler(self)
        self._http_server = HTTPServer((self.host, self.port), handler)

        def run_server() -> None:
            assert self._http_server is not None
            self._http_server.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None


def _handle_api(service: GooseService, path: str, method: str, body: Optional[bytes]) -> tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body or "{}") if body else {}
    except json.JSONDecodeError:
        return 400, {"error": "Invalid JSON"}

    if path == "/streams" and method == "POST":
        required = ["iface", "src_mac", "dst_mac", "app_id", "gocb_ref", "dat_set", "go_id"]
        for k in required:
            if k not in data:
                return 400, {"error": f"Missing field: {k}"}
        s = service.add_stream(data)
        return 201, _stream_to_dict(s)

    if path.startswith("/streams/") and path != "/streams/":
        stream_id = path.split("/", 2)[2].rstrip("/")
        if method == "GET":
            s = service.get_stream(stream_id)
            if s is None:
                return 404, {"error": "Not found"}
            return 200, _stream_to_dict(s)
        if method == "PATCH":
            s = service.modify_stream(stream_id, data)
            if s is None:
                return 404, {"error": "Not found"}
            return 200, _stream_to_dict(s)
        if method == "DELETE":
            ok = service.delete_stream(stream_id)
            if not ok:
                return 404, {"error": "Not found"}
            return 204, {}

    if path == "/streams" and method == "GET":
        streams = service.list_streams()
        return 200, {"streams": [_stream_to_dict(s) for s in streams]}

    return 404, {"error": "Not found"}


def make_handler(service: GooseService) -> type:
    """Crée une classe BaseHTTPRequestHandler liée au service."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PATCH(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def _dispatch(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            body = None
            if self.command in ("POST", "PATCH"):
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    body = self.rfile.read(length)
            status, result = _handle_api(service, path, self.command, body)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            if status != 204:
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler
