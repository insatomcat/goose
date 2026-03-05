from __future__ import annotations

import binascii
from dataclasses import dataclass
from typing import Callable, Optional

from scapy.all import (  # type: ignore[import-untyped]
    Ether,
    Raw,
    sendp,
    sniff,
)

from .codec import decode_goose_pdu, encode_goose_pdu
from .types import GooseFrame, GoosePDU


GOOSE_ETHERTYPE = 0x88B8


def _mac_str(mac: str) -> str:
    return mac.lower()


def _build_frame(
    dst_mac: str,
    src_mac: str,
    app_id: int,
    pdu: GoosePDU,
) -> bytes:
    payload = encode_goose_pdu(pdu)

    # En-tête GOOSE spécifique (APPID + longueur + reserved1 + reserved2)
    app_id_bytes = app_id.to_bytes(2, "big")
    length_bytes = (8 + len(payload)).to_bytes(2, "big")  # 8 octets de header GOOSE
    reserved = b"\x00\x00\x00\x00"
    goose_header = app_id_bytes + length_bytes + reserved

    eth = Ether(dst=_mac_str(dst_mac), src=_mac_str(src_mac), type=GOOSE_ETHERTYPE)
    pkt = eth / Raw(goose_header + payload)
    return bytes(pkt)


@dataclass
class GoosePublisher:
    """Publication de trames GOOSE sur un interface réseau."""

    iface: str
    src_mac: str
    app_id: int

    def send(
        self,
        dst_mac: str,
        pdu: GoosePDU,
        count: int = 1,
        inter: float = 0.0,
    ) -> None:
        """Envoie une ou plusieurs trames GOOSE."""
        raw = _build_frame(dst_mac=dst_mac, src_mac=self.src_mac, app_id=self.app_id, pdu=pdu)
        sendp(raw, iface=self.iface, count=count, inter=inter, verbose=False)


class GooseSubscriber:
    """Abstraction simple de souscripteur GOOSE basé sur scapy."""

    def __init__(
        self,
        iface: str,
        app_id: Optional[int] = None,
        callback: Optional[Callable[[GooseFrame], None]] = None,
    ) -> None:
        """
        - `iface` : nom de l'interface (ex: "eth0").
        - `app_id` : si renseigné, filtre les trames GOOSE sur cet APPID uniquement.
        - `callback` : fonction appelée pour chaque trame décodée.
        """
        self.iface = iface
        self.app_id = app_id
        self.callback = callback

    def _handle_pkt(self, pkt) -> None:  # type: ignore[no-untyped-def]
        if not pkt.haslayer(Ether):
            return
        eth = pkt[Ether]
        if eth.type != GOOSE_ETHERTYPE:
            return

        payload = bytes(eth.payload)
        if len(payload) < 8:
            return

        app_id = int.from_bytes(payload[0:2], "big")
        length = int.from_bytes(payload[2:4], "big")
        # reserved1 = payload[4:6]
        # reserved2 = payload[6:8]

        if self.app_id is not None and app_id != self.app_id:
            return

        goose_payload = payload[8:length]

        try:
            pdu = decode_goose_pdu(goose_payload)
        except Exception:
            pdu = None

        frame = GooseFrame(
            dst_mac=str(eth.dst),
            src_mac=str(eth.src),
            app_id=app_id,
            vlan_id=None,  # géré par scapy en amont si nécessaire
            ethertype=eth.type,
            raw_payload=goose_payload,
            pdu=pdu,
        )

        if self.callback:
            self.callback(frame)

    def start(self, count: int = 0, timeout: Optional[int] = None) -> None:
        """Démarre la capture (bloquante).

        - `count` : nombre maximum de trames à capturer (0 = illimité).
        - `timeout` : durée max en secondes (None = illimité).
        """

        sniff(
            iface=self.iface,
            prn=self._handle_pkt,
            store=False,
            timeout=timeout,
            count=count if count > 0 else 0,
        )


def decode_hex_goose(hex_str: str) -> GoosePDU:
    """Utilitaire : décode une chaîne hexadécimale représentant un APDU GOOSE."""
    hex_str = hex_str.replace(" ", "").replace("\n", "")
    data = binascii.unhexlify(hex_str)
    return decode_goose_pdu(data)

