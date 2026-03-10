#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime
from typing import Optional

# Ajoute la racine du dépôt au sys.path pour pouvoir importer goose61850
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goose61850 import GooseSubscriber
from goose61850.types import GooseFrame


def summarize_frame(frame: GooseFrame) -> str:
    pdu = frame.pdu
    if pdu is None:
        return (
            f"{frame.src_mac} -> {frame.dst_mac} "
            f"APPID=0x{frame.app_id:04X} (PDU non décodé)"
        )

    ts: Optional[datetime] = pdu.timestamp
    ts_str = ts.isoformat() if ts else "-"

    return (
        f"[{ts_str}] {frame.src_mac} -> {frame.dst_mac} "
        f"APPID=0x{frame.app_id:04X} "
        f"gocbRef={pdu.gocb_ref} "
        f"goID={pdu.go_id} "
        f"stNum={pdu.st_num} sqNum={pdu.sq_num} "
        f"confRev={pdu.conf_rev} "
        f"entries={pdu.num_dat_set_entries}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Écoute les trames GOOSE et affiche un résumé pour chacune.",
    )
    parser.add_argument(
        "iface",
        help="Interface réseau à écouter (ex: en0, eth0, ...)",
    )
    parser.add_argument(
        "--app-id",
        type=lambda x: int(x, 0),
        default=None,
        help="Filtre APPID (ex: 0x1000). Si omis, accepte tous les APPID.",
    )
    parser.add_argument(
        "--go-id",
        type=str,
        default=None,
        help="Filtre sur goID (chaîne exacte). Si omis, accepte tous les goID.",
    )

    args = parser.parse_args()

    def on_frame(frame: GooseFrame) -> None:
        if frame.pdu is None:
            # pas de filtre possible sur goID si PDU non décodé
            print(summarize_frame(frame))
            return

        if args.go_id is not None and frame.pdu.go_id != args.go_id:
            return

        print(summarize_frame(frame))

    sub = GooseSubscriber(
        iface=args.iface,
        app_id=args.app_id,
        callback=on_frame,
    )

    print(
        f"Écoute GOOSE sur {args.iface} "
        f"(APPID={'*' if args.app_id is None else hex(args.app_id)}, "
        f"goID={'*' if args.go_id is None else args.go_id})..."
    )
    print("Interrompre avec Ctrl+C.")

    sub.start()


if __name__ == "__main__":
    main()

