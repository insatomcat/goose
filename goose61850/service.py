"""Service GOOSE : envoi continu de flux GOOSE avec API HTTP."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7053,
        state_file: str | Path = "goose_streams.json",
        web_port: int = 7054,
    ) -> None:
        self.host = host
        self.port = port
        self._state_path = Path(state_file)
         # Port de l'interface web (HTML) simple
        self.web_port = web_port
        self._streams: Dict[str, GooseStream] = {}
        self._streams_lock = threading.Lock()
        self._stop = threading.Event()
        self._sender_thread: Optional[threading.Thread] = None
        self._http_server: Optional[HTTPServer] = None
        self._web_server: Optional[HTTPServer] = None
        # Charge l'état éventuel des flux depuis le disque.
        self._load_state()

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

        # Sauvegarde hors zone critique pour éviter les blocages.
        self._save_state()
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

        # Sauvegarde hors section critique.
        self._save_state()
        return s

    def delete_stream(self, stream_id: str) -> bool:
        with self._streams_lock:
            removed = self._streams.pop(stream_id, None) is not None

        if removed:
            self._save_state()
        return removed

    def get_stream(self, stream_id: str) -> Optional[GooseStream]:
        with self._streams_lock:
            return self._streams.get(stream_id)

    def list_streams(self) -> List[GooseStream]:
        with self._streams_lock:
            return list(self._streams.values())

    # ------------------------------------------------------------------
    # Persistance simple sur disque
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Sauvegarde la liste des flux dans un fichier JSON.

        Attention : cette fonction est appelée depuis des sections déjà
        protégées par `_streams_lock`. On évite donc toute ré‑entrée sur
        `list_streams()` et on manipule directement le dict interne.
        """
        try:
            with self._streams_lock:
                streams = [_stream_to_dict(s) for s in self._streams.values()]
            payload = {"streams": streams}
            tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self._state_path)
        except Exception:
            # On ne fait pas échouer le service si la persistance casse.
            pass

    def _load_state(self) -> None:
        """Recharge les flux depuis le fichier JSON, si présent."""
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return

        streams_data = raw.get("streams") or []
        now = time.monotonic()

        with self._streams_lock:
            self._streams.clear()
            for item in streams_data:
                try:
                    all_data = _parse_all_data(item.get("all_data", []))
                    s = GooseStream(
                        id=str(item["id"]),
                        iface=str(item["iface"]),
                        src_mac=str(item["src_mac"]),
                        dst_mac=str(item["dst_mac"]),
                        app_id=int(item["app_id"]),
                        vlan_id=item.get("vlan_id"),
                        vlan_priority=item.get("vlan_priority"),
                        gocb_ref=str(item["gocb_ref"]),
                        dat_set=str(item["dat_set"]),
                        go_id=str(item["go_id"]),
                        ttl=int(item.get("ttl", 5000)),
                        conf_rev=int(item.get("conf_rev", 1)),
                        simulation=bool(item.get("simulation", False)),
                        nds_com=bool(item.get("nds_com", False)),
                        all_data=all_data,
                        st_num=int(item.get("st_num", 1)),
                        sq_num=int(item.get("sq_num", 0)),
                        next_send_time=now,
                        current_interval_ms=float(max(self.IEC_MIN_MS, 1)),
                    )
                    self._streams[s.id] = s
                except Exception:
                    continue

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

        # API JSON
        handler = make_handler(self)
        self._http_server = HTTPServer((self.host, self.port), handler)

        def run_server() -> None:
            assert self._http_server is not None
            self._http_server.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()

        # Interface web (HTML) légère pour visualiser / modifier / supprimer
        web_handler = make_web_handler(self)
        self._web_server = HTTPServer((self.host, self.web_port), web_handler)

        def run_web() -> None:
            assert self._web_server is not None
            self._web_server.serve_forever()

        tw = threading.Thread(target=run_web, daemon=True)
        tw.start()

    def stop(self) -> None:
        self._stop.set()
        if self._http_server:
            self._http_server.shutdown()
            self._http_server = None
        if self._web_server:
            self._web_server.shutdown()
            self._web_server = None


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


def make_web_handler(service: GooseService) -> type:
    """Crée un handler HTTP servant une petite interface HTML."""

    class WebHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # type: ignore[override]
            parsed = urlparse(self.path)
            path = parsed.path or "/"

            if path == "/" or path == "/streams":
                self._render_streams_list()
            elif path.startswith("/streams/") and path.endswith("/edit"):
                # /streams/<id>/edit
                parts = path.split("/")
                if len(parts) >= 3 and parts[2]:
                    stream_id = parts[2]
                    self._render_edit(stream_id)
                else:
                    self._send_not_found()
            else:
                self._send_not_found()

        def do_POST(self) -> None:  # type: ignore[override]
            parsed = urlparse(self.path)
            path = parsed.path or "/"

            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            data = parse_qs(body.decode("utf-8")) if body else {}

            if path.startswith("/streams/") and path.endswith("/edit"):
                parts = path.split("/")
                if len(parts) >= 3 and parts[2]:
                    stream_id = parts[2]
                    self._handle_edit_post(stream_id, data)
                    return
            elif path.startswith("/streams/") and path.endswith("/delete"):
                parts = path.split("/")
                if len(parts) >= 3 and parts[2]:
                    stream_id = parts[2]
                    service.delete_stream(stream_id)
                    self._redirect("/streams")
                    return

            self._send_not_found()

        # --- Helpers HTML ---

        def _render_streams_list(self) -> None:
            streams = service.list_streams()
            rows = []
            for s in streams:
                rows.append(
                    f"<tr>"
                    f"<td>{s.id}</td>"
                    f"<td>{s.gocb_ref}</td>"
                    f"<td>{s.go_id}</td>"
                    f"<td>{s.st_num}</td>"
                    f"<td>{s.sq_num}</td>"
                    f"<td>"
                    f"<a href=\"/streams/{s.id}/edit\">Modifier</a>"
                    f" | "
                    f"<form method=\"POST\" action=\"/streams/{s.id}/delete\" style=\"display:inline\" onsubmit=\"return confirm('Supprimer ce flux ?');\">"
                    f"<button type=\"submit\">Supprimer</button>"
                    f"</form>"
                    f"</td>"
                    f"</tr>"
                )
            rows_html = "\n".join(rows) if rows else "<tr><td colspan=\"6\">Aucun flux</td></tr>"

            html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>GOOSE - Flux configurés</title>
  <style>
    body {{ font-family: sans-serif; margin: 1rem 2rem; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 1200px; }}
    th, td {{ border: 1px solid #ccc; padding: 0.3rem 0.5rem; text-align: left; }}
    th {{ background: #f0f0f0; }}
    a, button {{ font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>Flux GOOSE configurés</h1>
  <table>
    <thead>
      <tr>
        <th>ID</th>
        <th>gocbRef</th>
        <th>goID</th>
        <th>stNum</th>
        <th>sqNum</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
</body>
</html>
"""
            self._send_html(html)

        def _render_edit(self, stream_id: str) -> None:
            s = service.get_stream(stream_id)
            if s is None:
                self._send_not_found()
                return

            all_data_json = json.dumps(_stream_to_dict(s)["all_data"], ensure_ascii=False, indent=2)

            html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Modifier le flux {stream_id}</title>
  <style>
    body {{ font-family: sans-serif; margin: 1rem 2rem; max-width: 1000px; }}
    label {{ display: block; margin-top: 0.5rem; font-weight: bold; }}
    input[type=text], textarea {{ width: 100%; box-sizing: border-box; }}
    textarea {{ height: 10rem; font-family: monospace; font-size: 0.85rem; }}
    .readonly {{ background: #f5f5f5; }}
  </style>
</head>
<body>
  <h1>Modifier le flux</h1>
  <p><strong>ID:</strong> {s.id}</p>
  <p><strong>Interface:</strong> {s.iface} &nbsp; <strong>src_mac:</strong> {s.src_mac} &nbsp; <strong>dst_mac:</strong> {s.dst_mac}</p>
  <p><strong>APPID:</strong> 0x{s.app_id:04X}</p>

  <form method="POST" action="/streams/{s.id}/edit">
    <label>gocbRef</label>
    <input type="text" name="gocb_ref" value="{s.gocb_ref}" class="readonly" readonly>

    <label>datSet</label>
    <input type="text" name="dat_set" value="{s.dat_set}" class="readonly" readonly>

    <label>goID</label>
    <input type="text" name="go_id" value="{s.go_id}" class="readonly" readonly>

    <label>TTL (ms)</label>
    <input type="text" name="ttl" value="{s.ttl}">

    <label>allData (JSON, liste de valeurs et de ['raw', tag, hex])</label>
    <textarea name="all_data_json">{all_data_json}</textarea>

    <p>
      <button type="submit">Enregistrer</button>
      <a href="/streams">Annuler</a>
    </p>
  </form>
</body>
</html>
"""
            self._send_html(html)

        def _handle_edit_post(self, stream_id: str, data: Dict[str, List[str]]) -> None:
            updates: Dict[str, Any] = {}
            ttl_vals = data.get("ttl")
            if ttl_vals and ttl_vals[0].strip():
                try:
                    updates["ttl"] = int(ttl_vals[0].strip())
                except ValueError:
                    pass

            all_data_vals = data.get("all_data_json")
            if all_data_vals and all_data_vals[0].strip():
                try:
                    updates["all_data"] = json.loads(all_data_vals[0])
                except json.JSONDecodeError:
                    # Ne pas casser la requête pour un JSON incorrect, on ignore.
                    pass

            if updates:
                service.modify_stream(stream_id, updates)
            self._redirect("/streams")

        def _send_html(self, html: str, status: int = 200) -> None:
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def _send_not_found(self) -> None:
            self._send_html("<h1>404 Not Found</h1>", status=404)

        def log_message(self, format: str, *args: Any) -> None:  # type: ignore[override]
            pass

    return WebHandler
