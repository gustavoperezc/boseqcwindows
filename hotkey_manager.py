"""Global Windows hotkey registration for the Tkinter app."""

from __future__ import annotations

import ctypes
import queue
import re
import threading
from dataclasses import dataclass
from typing import Callable


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000


VK_CODES = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
}

for index in range(1, 25):
    VK_CODES[f"F{index}"] = 0x6F + index


class HotkeyError(RuntimeError):
    """Raised when a hotkey cannot be parsed or registered."""


@dataclass(frozen=True)
class ParsedHotkey:
    label: str
    modifiers: int
    vk: int


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


def parse_hotkey(value: str) -> ParsedHotkey:
    raw = (value or "").strip()
    if not raw:
        raise HotkeyError("Hotkey cannot be empty")

    parts = [part.strip() for part in re.split(r"\s*\+\s*", raw) if part.strip()]
    if len(parts) < 2:
        raise HotkeyError("Use at least one modifier, for example Ctrl+Alt+N")

    modifiers = 0
    key: str | None = None
    labels: list[str] = []

    for part in parts:
        token = part.upper().replace(" ", "")
        if token in {"CTRL", "CONTROL"}:
            modifiers |= MOD_CONTROL
            labels.append("Ctrl")
        elif token == "ALT":
            modifiers |= MOD_ALT
            labels.append("Alt")
        elif token == "SHIFT":
            modifiers |= MOD_SHIFT
            labels.append("Shift")
        elif token in {"WIN", "WINDOWS", "META", "CMD"}:
            modifiers |= MOD_WIN
            labels.append("Win")
        elif key is None:
            key = token
        else:
            raise HotkeyError("Only one non-modifier key is supported")

    if modifiers == 0:
        raise HotkeyError("Use Ctrl, Alt, Shift, or Win as a modifier")
    if key is None:
        raise HotkeyError("Missing key, for example Ctrl+Alt+N")

    vk = _key_to_vk(key)
    return ParsedHotkey(label="+".join(labels + [_format_key_label(key)]), modifiers=modifiers, vk=vk)


def _key_to_vk(key: str) -> int:
    if len(key) == 1 and key.isalpha():
        return ord(key)
    if len(key) == 1 and key.isdigit():
        return ord(key)
    if key in VK_CODES:
        return VK_CODES[key]
    raise HotkeyError(f"Unsupported key '{key}'. Try A-Z, 0-9, F1-F24, Space, or arrows.")


def _format_key_label(key: str) -> str:
    if len(key) == 1:
        return key.upper()
    return key.capitalize() if key.startswith("F") is False else key


class GlobalHotkey:
    """Register one system-wide hotkey using Win32 RegisterHotKey."""

    def __init__(self, callback: Callable[[], None], hotkey_id: int = 51035) -> None:
        self.callback = callback
        self.hotkey_id = hotkey_id
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._parsed: ParsedHotkey | None = None
        self._lock = threading.Lock()

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    @property
    def label(self) -> str | None:
        return self._parsed.label if self._parsed else None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, hotkey: str) -> str:
        parsed = parse_hotkey(hotkey)
        with self._lock:
            self.stop()
            ready_queue: queue.Queue[tuple[bool, str | int]] = queue.Queue(maxsize=1)
            thread = threading.Thread(
                target=self._message_loop,
                args=(parsed, ready_queue),
                name="BoseAncHotkey",
                daemon=True,
            )
            self._thread = thread
            thread.start()

            try:
                ok, value = ready_queue.get(timeout=2)
            except queue.Empty as exc:
                self._thread = None
                raise HotkeyError("Timed out while registering the hotkey") from exc

            if not ok:
                self._thread = None
                raise HotkeyError(str(value))

            self._thread_id = int(value)
            self._parsed = parsed
            return parsed.label

    def stop(self) -> None:
        thread = self._thread
        thread_id = self._thread_id
        if thread and thread.is_alive() and thread_id:
            self._user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            thread.join(timeout=2)
        self._thread = None
        self._thread_id = None
        self._parsed = None

    def _message_loop(self, parsed: ParsedHotkey, ready_queue: queue.Queue[tuple[bool, str | int]]) -> None:
        thread_id = self._kernel32.GetCurrentThreadId()
        msg = _MSG()
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

        modifiers = parsed.modifiers | MOD_NOREPEAT
        if not self._user32.RegisterHotKey(None, self.hotkey_id, modifiers, parsed.vk):
            ready_queue.put((False, f"Could not register {parsed.label}: Win32 error {ctypes.get_last_error()}"))
            return

        ready_queue.put((True, thread_id))
        try:
            while True:
                result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    break
                if msg.message == WM_HOTKEY and int(msg.wParam) == self.hotkey_id:
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            self._user32.UnregisterHotKey(None, self.hotkey_id)

    def _configure_api(self) -> None:
        self._user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        self._user32.RegisterHotKey.restype = ctypes.c_bool
        self._user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = ctypes.c_bool
        self._user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.PeekMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        self._user32.PeekMessageW.restype = ctypes.c_bool
        self._user32.PostThreadMessageW.argtypes = [ctypes.c_uint32, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        self._user32.PostThreadMessageW.restype = ctypes.c_bool
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
