"""Minimal Bose BMAP protocol helpers for QC35 / QC35 II ANR control.

The QC35 family exposes Active Noise Reduction (ANR) at BMAP address [1.6].
The SETGET payload is one byte:
  0 = off, 1 = high, 2 = wind, 3 = low
"""

from __future__ import annotations

from dataclasses import dataclass


OP_SET = 0
OP_GET = 1
OP_SETGET = 2
OP_STATUS = 3
OP_ERROR = 4
OP_START = 5
OP_RESULT = 6
OP_PROCESSING = 7

OP_NAMES = {
    OP_SET: "SET",
    OP_GET: "GET",
    OP_SETGET: "SETGET",
    OP_STATUS: "STATUS",
    OP_ERROR: "ERROR",
    OP_START: "START",
    OP_RESULT: "RESULT",
    OP_PROCESSING: "PROCESSING",
}

ERROR_NAMES = {
    0: "Unknown",
    1: "Length",
    2: "Checksum",
    3: "FblockNotSupported",
    4: "FunctionNotSupported",
    5: "OperatorNotSupportedOrAuthRequired",
    6: "InvalidData",
    7: "DataUnavailable",
    8: "Runtime",
    9: "Timeout",
    10: "InvalidState",
    15: "InvalidTransition",
    20: "InsecureTransport",
}

ANR_VALUES = {"off": 0, "high": 1, "wind": 2, "low": 3}
ANR_NAMES = {value: name for name, value in ANR_VALUES.items()}

QC35_INIT_ADDRESS = (0, 1)
QC35_ANR_ADDRESS = (1, 6)


class BoseProtocolError(RuntimeError):
    """Raised when a BMAP response reports an error or cannot be parsed."""


@dataclass(frozen=True)
class BmapResponse:
    fblock: int
    function: int
    operator: int
    payload: bytes

    @property
    def operator_name(self) -> str:
        return OP_NAMES.get(self.operator, f"OP_{self.operator}")

    def is_error(self) -> bool:
        return self.operator == OP_ERROR


@dataclass(frozen=True)
class AncCommandResult:
    requested_mode: str
    packet: bytes
    raw_response: bytes
    responses: tuple[BmapResponse, ...]
    reported_mode: str | None


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def bmap_packet(fblock: int, function: int, operator: int, payload: bytes = b"") -> bytes:
    if not 0 <= fblock <= 0xFF:
        raise ValueError("fblock must be 0..255")
    if not 0 <= function <= 0xFF:
        raise ValueError("function must be 0..255")
    if not 0 <= operator <= 0x0F:
        raise ValueError("operator must be 0..15")
    if len(payload) > 0xFF:
        raise ValueError("payload is too large for one BMAP packet")

    flags = operator & 0x0F
    return bytes([fblock, function, flags, len(payload)]) + payload


def parse_response(data: bytes) -> BmapResponse | None:
    if len(data) < 4:
        return None
    length = data[3]
    if len(data) < 4 + length:
        return None
    return BmapResponse(
        fblock=data[0],
        function=data[1],
        operator=data[2] & 0x0F,
        payload=data[4:4 + length],
    )


def parse_all_responses(data: bytes) -> tuple[BmapResponse, ...]:
    responses: list[BmapResponse] = []
    offset = 0
    while offset + 4 <= len(data):
        length = data[offset + 3]
        end = offset + 4 + length
        if end > len(data):
            break
        responses.append(
            BmapResponse(
                fblock=data[offset],
                function=data[offset + 1],
                operator=data[offset + 2] & 0x0F,
                payload=data[offset + 4:end],
            )
        )
        offset = end
    return tuple(responses)


def format_response(response: BmapResponse) -> str:
    payload_hex = bytes_to_hex(response.payload) if response.payload else "<empty>"
    if response.operator == OP_ERROR and response.payload:
        code = response.payload[0]
        name = ERROR_NAMES.get(code, f"Error_{code}")
        return f"[{response.fblock}.{response.function}] ERROR {name} ({payload_hex})"
    return f"[{response.fblock}.{response.function}] {response.operator_name} {payload_hex}"


def raise_for_bmap_errors(responses: tuple[BmapResponse, ...]) -> None:
    for response in responses:
        if response.is_error():
            raise BoseProtocolError(format_response(response))


def build_qc35_init_packet() -> bytes:
    """QC35/QC35 II usually expects GET [0.1] before other commands."""
    return bmap_packet(*QC35_INIT_ADDRESS, OP_GET)


def build_get_anc_packet() -> bytes:
    return bmap_packet(*QC35_ANR_ADDRESS, OP_GET)


def build_set_anc_packet(mode: str) -> bytes:
    normalized = mode.lower().strip()
    if normalized not in ANR_VALUES:
        valid = ", ".join(sorted(ANR_VALUES))
        raise ValueError(f"Unsupported ANR mode '{mode}'. Valid modes: {valid}")
    payload = bytes([ANR_VALUES[normalized]])
    return bmap_packet(*QC35_ANR_ADDRESS, OP_SETGET, payload)


def parse_anc_mode(payload: bytes) -> str | None:
    if not payload:
        return None
    return ANR_NAMES.get(payload[0], f"unknown({payload[0]})")


def initialize_qc35(client) -> tuple[BmapResponse, ...]:
    raw = client.send_packet(build_qc35_init_packet(), expect_response=True)
    responses = parse_all_responses(raw)
    raise_for_bmap_errors(responses)
    return responses


def read_anc(client) -> str | None:
    raw = client.send_packet(build_get_anc_packet(), expect_response=True)
    responses = parse_all_responses(raw)
    raise_for_bmap_errors(responses)
    for response in responses:
        if (response.fblock, response.function) == QC35_ANR_ADDRESS and response.payload:
            return parse_anc_mode(response.payload)
    return None


def set_anc(client, mode: str) -> AncCommandResult:
    normalized = mode.lower().strip()
    packet = build_set_anc_packet(normalized)
    raw = client.send_packet(packet, expect_response=True)
    responses = parse_all_responses(raw)
    raise_for_bmap_errors(responses)

    reported_mode = None
    for response in responses:
        if (response.fblock, response.function) == QC35_ANR_ADDRESS and response.payload:
            reported_mode = parse_anc_mode(response.payload)
            break

    return AncCommandResult(
        requested_mode=normalized,
        packet=packet,
        raw_response=raw,
        responses=responses,
        reported_mode=reported_mode,
    )


def set_anc_high(client) -> AncCommandResult:
    return set_anc(client, "high")


def set_anc_low(client) -> AncCommandResult:
    return set_anc(client, "low")


def set_anc_off(client) -> AncCommandResult:
    return set_anc(client, "off")
