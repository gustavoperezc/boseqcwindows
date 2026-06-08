"""Windows-focused Bluetooth RFCOMM client for Bose BMAP packets."""

from __future__ import annotations

import ctypes
import json
import logging
import platform
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable


LOGGER = logging.getLogger(__name__)

DEFAULT_QC35_RFCOMM_CHANNEL = 8
BOSE_NAME_HINTS = ("bose", "quietcomfort", "qc35", "qc 35")


class BluetoothClientError(RuntimeError):
    """Base exception for Bluetooth transport errors."""


class BluetoothUnavailableError(BluetoothClientError):
    """Raised when no usable Bluetooth backend is available."""


class DeviceNotFoundError(BluetoothClientError):
    """Raised when a requested Bose device cannot be found."""


class RfcommConnectionError(BluetoothClientError):
    """Raised when an RFCOMM connection cannot be opened."""


class RfcommTimeoutError(BluetoothClientError):
    """Raised when a connected device does not respond in time."""


@dataclass(frozen=True)
class BluetoothDevice:
    name: str
    address: str | None
    source: str
    status: str | None = None

    def label(self) -> str:
        address = self.address or "MAC unknown"
        status = f", {self.status}" if self.status else ""
        return f"{self.name} ({address}, {self.source}{status})"


def format_mac(address: str) -> str:
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", address or "")
    if len(cleaned) != 12:
        raise ValueError("Bluetooth MAC must have 12 hex digits")
    return ":".join(cleaned[i:i + 2].upper() for i in range(0, 12, 2))


def is_bose_name(name: str | None) -> bool:
    text = (name or "").lower()
    return any(hint in text for hint in BOSE_NAME_HINTS)


def discover_bose_devices(scan_nearby: bool = False) -> list[BluetoothDevice]:
    """Return likely Bose QC35/QC35 II devices known to Windows.

    Windows does not expose one perfect "paired devices with MAC" API to
    Python, so this combines PnP data with optional PyBluez discovery.
    """
    devices: list[BluetoothDevice] = []

    if platform.system() == "Windows":
        devices.extend(_discover_windows_pnp_bose_devices())

    if scan_nearby:
        devices.extend(_discover_pybluez_bose_devices())

    return _dedupe_devices(devices)


def _discover_windows_pnp_bose_devices() -> list[BluetoothDevice]:
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        (
            "Get-PnpDevice -Class Bluetooth | "
            "Select-Object FriendlyName,InstanceId,Status | "
            "ConvertTo-Json -Compress"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=_subprocess_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.debug("Windows PnP Bluetooth discovery failed: %s", exc)
        return []

    if result.returncode != 0 or not result.stdout.strip():
        LOGGER.debug("Windows PnP Bluetooth discovery returned no data: %s", result.stderr)
        return []

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        LOGGER.debug("Windows PnP Bluetooth JSON parse failed: %s", exc)
        return []

    rows = payload if isinstance(payload, list) else [payload]
    devices: list[BluetoothDevice] = []
    for row in rows:
        name = str(row.get("FriendlyName") or "").strip()
        instance_id = str(row.get("InstanceId") or "")
        if not is_bose_name(name):
            continue
        devices.append(
            BluetoothDevice(
                name=name,
                address=_extract_mac_from_instance_id(instance_id),
                source="Windows PnP",
                status=str(row.get("Status") or "").strip() or None,
            )
        )
    return devices


def _subprocess_creationflags() -> int:
    if platform.system() != "Windows":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _extract_mac_from_instance_id(instance_id: str) -> str | None:
    candidates = re.findall(r"(?i)(?<![0-9A-F])([0-9A-F]{12})(?![0-9A-F])", instance_id)
    if not candidates:
        candidates = re.findall(r"(?i)DEV_([0-9A-F]{12})", instance_id)
    if not candidates:
        return None
    try:
        return format_mac(candidates[-1])
    except ValueError:
        return None


def _discover_pybluez_bose_devices() -> list[BluetoothDevice]:
    try:
        import bluetooth  # type: ignore
    except ImportError:
        return []

    try:
        nearby = bluetooth.discover_devices(duration=6, lookup_names=True)
    except Exception as exc:  # PyBluez raises backend-specific exceptions.
        LOGGER.debug("PyBluez discovery failed: %s", exc)
        return []

    devices: list[BluetoothDevice] = []
    for address, name in nearby:
        if is_bose_name(name):
            devices.append(
                BluetoothDevice(
                    name=name or "Bose device",
                    address=format_mac(address),
                    source="PyBluez scan",
                )
            )
    return devices


def _dedupe_devices(devices: Iterable[BluetoothDevice]) -> list[BluetoothDevice]:
    by_key: dict[str, BluetoothDevice] = {}
    for device in devices:
        key = device.address or f"name:{device.name.lower()}:{device.source}"
        if key not in by_key or (by_key[key].address is None and device.address):
            by_key[key] = device
    return sorted(by_key.values(), key=lambda item: (item.address is None, item.name.lower()))


def available_backends() -> list[str]:
    names = ["auto"]
    if platform.system() == "Windows":
        names.append("winsock")
    if _python_socket_supports_bluetooth():
        names.append("python-socket")
    if _pybluez_available():
        names.append("pybluez")
    return names


def _python_socket_supports_bluetooth() -> bool:
    return all(hasattr(socket, attr) for attr in ("AF_BLUETOOTH", "BTPROTO_RFCOMM"))


def _pybluez_available() -> bool:
    try:
        import bluetooth  # noqa: F401
    except ImportError:
        return False
    return True


class BoseBluetoothClient:
    """Persistent RFCOMM connection used by the GUI and protocol layer."""

    def __init__(
        self,
        address: str,
        channel: int = DEFAULT_QC35_RFCOMM_CHANNEL,
        timeout: float = 3.0,
        backend: str = "auto",
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.address = format_mac(address)
        self.channel = int(channel)
        self.timeout = float(timeout)
        self.backend = backend
        self.log_callback = log_callback
        self._socket = None
        self._backend_name: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._socket is not None

    @property
    def backend_name(self) -> str | None:
        return self._backend_name

    def connect(self) -> None:
        if self.is_connected:
            return

        last_error: Exception | None = None
        for backend_name in self._backend_candidates():
            try:
                self._log(f"Connecting to {self.address} on RFCOMM channel {self.channel} using {backend_name}")
                backend_socket = _create_backend_socket(
                    backend_name,
                    self.address,
                    self.channel,
                    self.timeout,
                )
                backend_socket.connect()
                self._socket = backend_socket
                self._backend_name = backend_name
                self._log(f"Connected with backend {backend_name}")
                return
            except Exception as exc:
                last_error = exc
                self._log(f"{backend_name} failed: {exc}")

        raise RfcommConnectionError(
            "Could not open RFCOMM connection. "
            "Check that Bluetooth is on, the headphones are paired, powered on, "
            f"and channel {self.channel} is correct. Last error: {last_error}"
        )

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
            self._backend_name = None
            self._log("Connection closed")

    def send_packet(self, packet: bytes, expect_response: bool = True) -> bytes:
        if not self._socket:
            raise RfcommConnectionError("Not connected")

        self._log(f"TX {packet.hex(' ').upper()}")
        self._socket.send_all(packet)

        if not expect_response:
            return b""

        time.sleep(0.15)
        response = self._socket.recv(4096)
        self._log(f"RX {response.hex(' ').upper() if response else '<empty>'}")
        return response

    def _backend_candidates(self) -> list[str]:
        if self.backend != "auto":
            return [self.backend]
        candidates: list[str] = []
        if platform.system() == "Windows":
            candidates.append("winsock")
        if _python_socket_supports_bluetooth():
            candidates.append("python-socket")
        candidates.append("pybluez")
        return candidates

    def _log(self, message: str) -> None:
        LOGGER.info(message)
        if self.log_callback:
            self.log_callback(message)

    def __enter__(self) -> "BoseBluetoothClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _create_backend_socket(name: str, address: str, channel: int, timeout: float):
    if name == "winsock":
        if platform.system() != "Windows":
            raise BluetoothUnavailableError("winsock backend is Windows-only")
        return _WindowsWinsockRfcommSocket(address, channel, timeout)
    if name == "python-socket":
        return _PythonSocketRfcommSocket(address, channel, timeout)
    if name == "pybluez":
        return _PyBluezRfcommSocket(address, channel, timeout)
    raise BluetoothUnavailableError(f"Unknown backend: {name}")


class _PythonSocketRfcommSocket:
    def __init__(self, address: str, channel: int, timeout: float) -> None:
        if not _python_socket_supports_bluetooth():
            raise BluetoothUnavailableError("Python socket has no AF_BLUETOOTH/RFCOMM support")
        self.address = address
        self.channel = channel
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        try:
            self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.address, self.channel))
        except OSError as exc:
            self.close()
            raise RfcommConnectionError(str(exc)) from exc

    def send_all(self, data: bytes) -> None:
        if not self.sock:
            raise RfcommConnectionError("Socket is not connected")
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise RfcommConnectionError(str(exc)) from exc

    def recv(self, size: int) -> bytes:
        if not self.sock:
            raise RfcommConnectionError("Socket is not connected")
        try:
            return self.sock.recv(size)
        except socket.timeout as exc:
            raise RfcommTimeoutError("No response from device") from exc
        except OSError as exc:
            raise RfcommConnectionError(str(exc)) from exc

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None


class _PyBluezRfcommSocket:
    def __init__(self, address: str, channel: int, timeout: float) -> None:
        try:
            import bluetooth  # type: ignore
        except ImportError as exc:
            raise BluetoothUnavailableError("PyBluez/pybluez2 is not installed") from exc
        self.bluetooth = bluetooth
        self.address = address
        self.channel = channel
        self.timeout = timeout
        self.sock = None

    def connect(self) -> None:
        try:
            self.sock = self.bluetooth.BluetoothSocket(self.bluetooth.RFCOMM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.address, self.channel))
        except Exception as exc:
            self.close()
            raise RfcommConnectionError(str(exc)) from exc

    def send_all(self, data: bytes) -> None:
        if not self.sock:
            raise RfcommConnectionError("Socket is not connected")
        try:
            self.sock.send(data)
        except Exception as exc:
            raise RfcommConnectionError(str(exc)) from exc

    def recv(self, size: int) -> bytes:
        if not self.sock:
            raise RfcommConnectionError("Socket is not connected")
        try:
            return self.sock.recv(size)
        except Exception as exc:
            raise RfcommTimeoutError(str(exc)) from exc

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None


class _WindowsWinsockRfcommSocket:
    AF_BTH = 32
    BTHPROTO_RFCOMM = 3
    SOCK_STREAM = 1
    SOL_SOCKET = 0xFFFF
    SO_SNDTIMEO = 0x1005
    SO_RCVTIMEO = 0x1006
    SOCKET_ERROR = -1
    WSAETIMEDOUT = 10060

    _started = False
    _socket_type = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
    _invalid_socket = (2 ** (ctypes.sizeof(_socket_type) * 8)) - 1

    def __init__(self, address: str, channel: int, timeout: float) -> None:
        self.address = address
        self.channel = int(channel)
        self.timeout = timeout
        self.sock: int | None = None
        self.ws2_32 = ctypes.WinDLL("Ws2_32.dll")
        self._configure_api()

    def _configure_api(self) -> None:
        socket_type = self._socket_type
        self.ws2_32.WSAStartup.argtypes = [ctypes.c_ushort, ctypes.c_void_p]
        self.ws2_32.WSAStartup.restype = ctypes.c_int
        self.ws2_32.WSAGetLastError.argtypes = []
        self.ws2_32.WSAGetLastError.restype = ctypes.c_int
        self.ws2_32.WSASocketW.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.ws2_32.WSASocketW.restype = socket_type
        self.ws2_32.connect.argtypes = [socket_type, ctypes.c_void_p, ctypes.c_int]
        self.ws2_32.connect.restype = ctypes.c_int
        self.ws2_32.send.argtypes = [socket_type, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.ws2_32.send.restype = ctypes.c_int
        self.ws2_32.recv.argtypes = [socket_type, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.ws2_32.recv.restype = ctypes.c_int
        self.ws2_32.closesocket.argtypes = [socket_type]
        self.ws2_32.closesocket.restype = ctypes.c_int
        self.ws2_32.setsockopt.argtypes = [socket_type, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.ws2_32.setsockopt.restype = ctypes.c_int

    def connect(self) -> None:
        self._ensure_winsock_started()
        sock = self.ws2_32.WSASocketW(
            self.AF_BTH,
            self.SOCK_STREAM,
            self.BTHPROTO_RFCOMM,
            None,
            0,
            0,
        )
        if int(sock) == self._invalid_socket:
            raise RfcommConnectionError(self._last_error("WSASocketW failed"))
        self.sock = int(sock)
        self._set_timeouts()

        sockaddr = _SOCKADDR_BTH()
        sockaddr.addressFamily = self.AF_BTH
        sockaddr.btAddr = _mac_to_bth_addr(self.address)
        sockaddr.serviceClassId = _GUID()
        sockaddr.port = self.channel

        result = self.ws2_32.connect(
            self._socket_type(self.sock),
            ctypes.byref(sockaddr),
            ctypes.sizeof(sockaddr),
        )
        if result == self.SOCKET_ERROR:
            error = self._last_error("connect failed")
            self.close()
            raise RfcommConnectionError(error)

    def send_all(self, data: bytes) -> None:
        if self.sock is None:
            raise RfcommConnectionError("Socket is not connected")
        sent_total = 0
        while sent_total < len(data):
            chunk = data[sent_total:]
            buffer = ctypes.create_string_buffer(chunk)
            sent = self.ws2_32.send(self._socket_type(self.sock), buffer, len(chunk), 0)
            if sent == self.SOCKET_ERROR:
                raise RfcommConnectionError(self._last_error("send failed"))
            sent_total += sent

    def recv(self, size: int) -> bytes:
        if self.sock is None:
            raise RfcommConnectionError("Socket is not connected")
        buffer = ctypes.create_string_buffer(size)
        received = self.ws2_32.recv(self._socket_type(self.sock), buffer, size, 0)
        if received == self.SOCKET_ERROR:
            error_code = self.ws2_32.WSAGetLastError()
            if error_code == self.WSAETIMEDOUT:
                raise RfcommTimeoutError("No response from device")
            raise RfcommConnectionError(f"recv failed: WSA error {error_code}")
        if received == 0:
            raise RfcommConnectionError("Device closed the RFCOMM socket")
        return buffer.raw[:received]

    def close(self) -> None:
        if self.sock is not None:
            self.ws2_32.closesocket(self._socket_type(self.sock))
            self.sock = None

    def _ensure_winsock_started(self) -> None:
        if self.__class__._started:
            return
        wsadata = ctypes.create_string_buffer(512)
        result = self.ws2_32.WSAStartup(0x0202, wsadata)
        if result != 0:
            raise BluetoothUnavailableError(f"WSAStartup failed: {result}")
        self.__class__._started = True

    def _set_timeouts(self) -> None:
        if self.sock is None:
            return
        timeout_ms = ctypes.c_uint32(max(1, int(self.timeout * 1000)))
        for option in (self.SO_SNDTIMEO, self.SO_RCVTIMEO):
            self.ws2_32.setsockopt(
                self._socket_type(self.sock),
                self.SOL_SOCKET,
                option,
                ctypes.byref(timeout_ms),
                ctypes.sizeof(timeout_ms),
            )

    def _last_error(self, prefix: str) -> str:
        return f"{prefix}: WSA error {self.ws2_32.WSAGetLastError()}"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SOCKADDR_BTH(ctypes.Structure):
    _fields_ = [
        ("addressFamily", ctypes.c_ushort),
        ("btAddr", ctypes.c_uint64),
        ("serviceClassId", _GUID),
        ("port", ctypes.c_uint32),
    ]


def _mac_to_bth_addr(address: str) -> int:
    return int(format_mac(address).replace(":", ""), 16)


def environment_summary() -> list[str]:
    lines = [
        f"Platform: {platform.platform()}",
        f"Python: {sys.version.split()[0]}",
        f"Backends: {', '.join(available_backends())}",
        f"Default QC35 RFCOMM channel: {DEFAULT_QC35_RFCOMM_CHANNEL}",
    ]
    if platform.system() == "Windows":
        lines.append("Windows native backend: Winsock AF_BTH via ctypes")
    if not _pybluez_available():
        lines.append("PyBluez/pybluez2: not installed (optional)")
    return lines
