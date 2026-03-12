#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import List, Tuple

# Ajoute la racine du dépôt au sys.path pour pouvoir importer goose61850
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goose61850 import GoosePDU, GoosePublisher  # type: ignore[import-not-found]


def build_pdu_from_args(args: argparse.Namespace) -> GoosePDU:
    all_data: List[object] = []

    # Nouveau mode : --value TYPE:VALEUR (préserve strictement l'ordre des FCDA).
    # TYPE ∈ {b,bool,i,int,s,str}. Exemple :
    #   --value b:1 --value i:0 --value s:OR1
    if getattr(args, "value", None):
        typed_values: List[Tuple[str, str]] = []
        for raw in args.value:
            if ":" not in raw:
                raise ValueError(f"Valeur --value invalide (attendu TYPE:VALEUR) : {raw!r}")
            type_prefix, val = raw.split(":", 1)
            typed_values.append((type_prefix.strip().lower(), val))

        for t, v in typed_values:
            if t in ("b", "bool"):
                all_data.append(v.lower() in ("1", "true", "t", "yes", "y"))
            elif t in ("i", "int"):
                all_data.append(int(v, 0))
            elif t in ("s", "str"):
                all_data.append(v)
            else:
                raise ValueError(f"Type --value inconnu : {t!r} (attendu b/bool/i/int/s/str)")

    # Ancien mode (compatibilité) : --bool / --int / --str, regroupés par type.
    # Si --value est fourni, ces options supplémentaires sont ignorées.
    elif args.bool is not None or args.int is not None or args.str is not None:
        if args.bool is not None:
            for v in args.bool:
                all_data.append(v.lower() in ("1", "true", "t", "yes", "y"))

        if args.int is not None:
            for v in args.int:
                all_data.append(int(v, 0))

        if args.str is not None:
            for v in args.str:
                all_data.append(v)

    now = datetime.now(timezone.utc)

    return GoosePDU(
        gocb_ref=args.gocb_ref,
        time_allowed_to_live=args.ttl,
        dat_set=args.dat_set,
        go_id=args.go_id,
        timestamp=now,
        st_num=args.st_num,
        sq_num=args.sq_num,
        simulation=args.sim,
        conf_rev=args.conf_rev,
        nds_com=args.nds_com,
        num_dat_set_entries=args.entries if args.entries is not None else len(all_data),
        all_data=all_data,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Envoie une trame GOOSE configurable sur le réseau.",
    )

    # Interface / MAC / VLAN / APPID
    parser.add_argument("iface", help="Interface réseau (ex: processbus, en0, eth0, ...)")
    parser.add_argument("src_mac", help="Adresse MAC source utilisée pour l'émission.")
    parser.add_argument("dst_mac", help="Adresse MAC destination (souvent multicast GOOSE).")

    parser.add_argument(
        "--appid",
        type=lambda x: int(x, 0),
        required=True,
        help="APPID GOOSE (ex: 0x0600).",
    )
    parser.add_argument(
        "--vlan-id",
        type=int,
        default=None,
        help="VLAN ID (802.1Q). Si omis, trame non tagguée.",
    )
    parser.add_argument(
        "--vlan-priority",
        type=int,
        default=None,
        help="Priorité VLAN (0-7). Si omis, priorité 0.",
    )

    # Champs GOOSE principaux
    parser.add_argument(
        "--gocb-ref",
        required=True,
        help="gocbRef (ex: VMC7_6LD0/LLN0$GO$CB_LDPHAS1_GME_DEP6).",
    )
    parser.add_argument(
        "--dat-set",
        required=True,
        help="datSet (ex: VMC7_6LD0/LLN0$DS_LDPHAS1_GME_DEP6).",
    )
    parser.add_argument(
        "--go-id",
        required=True,
        help="goID (ex: LDPHAS1_GME_DEP6_S).",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=5000,
        help="timeAllowedToLive en ms (par défaut: 5000).",
    )
    parser.add_argument(
        "--st-num",
        type=int,
        default=1,
        help="stNum (par défaut: 1).",
    )
    parser.add_argument(
        "--sq-num",
        type=int,
        default=0,
        help="sqNum (par défaut: 0).",
    )
    parser.add_argument(
        "--conf-rev",
        type=int,
        default=1,
        help="confRev (par défaut: 1).",
    )
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Active le flag simulation/test.",
    )
    parser.add_argument(
        "--nds-com",
        action="store_true",
        help="Active le flag ndsCom.",
    )
    parser.add_argument(
        "--entries",
        type=int,
        default=None,
        help="numDatSetEntries. Par défaut = len(allData).",
    )

    # Contenu de allData
    parser.add_argument(
        "--value",
        action="append",
        help=(
            "Ajoute une valeur typée à allData en respectant l'ordre (répétable). "
            "Format: TYPE:VALEUR avec TYPE ∈ {b,bool,i,int,s,str}, "
            "ex: --value b:1 --value i:0 --value s:OR1. "
            "Si --value est utilisé, les options --bool/--int/--str sont ignorées."
        ),
    )
    parser.add_argument(
        "--bool",
        action="append",
        help="Ajoute une valeur booléenne à allData (true/false, utilisable plusieurs fois).",
    )
    parser.add_argument(
        "--int",
        action="append",
        help="Ajoute un entier (décimal ou 0x..) à allData (utilisable plusieurs fois).",
    )
    parser.add_argument(
        "--str",
        action="append",
        help="Ajoute une chaîne à allData (utilisable plusieurs fois).",
    )

    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Nombre de trames à envoyer (par défaut: 1).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Intervalle entre trames (en secondes).",
    )
    parser.add_argument(
        "--auto-sq",
        action="store_true",
        help="Incrémente automatiquement sqNum à chaque trame envoyée.",
    )
    parser.add_argument(
        "--iec-profile",
        action="store_true",
        help=(
            "Applique une loi de temporisation IEC 61850 simple "
            "(intervalle qui double entre --iec-min-ms et --iec-max-ms). "
            "Utilisé uniquement avec --auto-sq."
        ),
    )
    parser.add_argument(
        "--iec-min-ms",
        type=int,
        default=10,
        help="Intervalle minimal IEC en millisecondes (par défaut: 10 ms).",
    )
    parser.add_argument(
        "--iec-max-ms",
        type=int,
        default=2000,
        help="Intervalle maximal IEC en millisecondes (par défaut: 2000 ms).",
    )

    args = parser.parse_args()

    pdu = build_pdu_from_args(args)

    publisher = GoosePublisher(
        iface=args.iface,
        src_mac=args.src_mac,
        app_id=args.appid,
        vlan_id=args.vlan_id,
        vlan_priority=args.vlan_priority,
    )

    print(
        f"Envoi GOOSE sur {args.iface} "
        f"src_mac={args.src_mac} dst_mac={args.dst_mac} "
        f"APPID=0x{args.appid:04X} vlan={args.vlan_id if args.vlan_id is not None else '-'} "
        f"gocbRef={args.gocb_ref} goID={args.go_id} count={args.count}",
    )
    # Mode simple : on laisse scapy gérer le count/inter, sqNum reste fixe.
    if not args.auto_sq:
        publisher.send(
            dst_mac=args.dst_mac,
            pdu=pdu,
            count=args.count,
            inter=args.interval,
        )
    else:
        # Mode GOOSE-like : on incrémente sqNum à chaque trame.
        base_sq = args.sq_num

        if args.iec_profile:
            # Profil simple IEC 61850: on part de iec-min, puis on double jusqu'à iec-max.
            current_interval = max(args.iec_min_ms, 1) / 1000.0
            max_interval = max(args.iec_max_ms, args.iec_min_ms) / 1000.0

            for i in range(args.count):
                pdu.sq_num = base_sq + i
                publisher.send(
                    dst_mac=args.dst_mac,
                    pdu=pdu,
                    count=1,
                    inter=0.0,
                )
                if i < args.count - 1:
                    time.sleep(current_interval)
                    current_interval = min(current_interval * 2.0, max_interval)
        else:
            for i in range(args.count):
                pdu.sq_num = base_sq + i
                publisher.send(
                    dst_mac=args.dst_mac,
                    pdu=pdu,
                    count=1,
                    inter=0.0,
                )
                if args.interval > 0 and i < args.count - 1:
                    time.sleep(args.interval)


if __name__ == "__main__":
    main()

