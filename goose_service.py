#!/usr/bin/env python3
"""Service GOOSE : point d'entrée principal (API HTTP + envoi continu)."""
from __future__ import annotations

import argparse
import pathlib
import signal
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goose61850.service import GooseService  # type: ignore[import-not-found]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Démarre le service GOOSE (API HTTP + envoi continu des flux).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Adresse d'écoute de l'API HTTP (défaut: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9843,
        help="Port d'écoute de l'API HTTP (défaut: 9843).",
    )
    args = parser.parse_args()

    service = GooseService(host=args.host, port=args.port)
    service.start()

    stop_event = threading.Event()

    def on_signal(sig: int, frame: object) -> None:
        service.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print(f"Service GOOSE démarré. API HTTP sur http://{args.host}:{args.port}")
    print("  POST   /streams     - Ajouter un flux")
    print("  GET    /streams     - Lister les flux")
    print("  GET    /streams/id  - Détail d'un flux")
    print("  PATCH  /streams/id  - Modifier un flux")
    print("  DELETE /streams/id  - Supprimer un flux")
    print("Interrompre avec Ctrl+C.")

    stop_event.wait()


if __name__ == "__main__":
    main()
