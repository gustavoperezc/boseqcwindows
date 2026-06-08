"""Tkinter GUI for controlling Bose QC35 / QC35 II ANR modes."""

from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import bose_protocol
from bluetooth_client import (
    DEFAULT_QC35_RFCOMM_CHANNEL,
    BoseBluetoothClient,
    BluetoothDevice,
    available_backends,
    discover_bose_devices,
    environment_summary,
    format_mac,
)
from hotkey_manager import GlobalHotkey


LOGGER = logging.getLogger(__name__)
CONFIG_PATH = Path("config.json")
ANC_CYCLE = ("high", "low", "off")


class BoseAncApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Bose QC35 II ANC Control")
        self.geometry("760x560")
        self.minsize(680, 500)

        self.config_data = self._load_config()
        self.devices: list[BluetoothDevice] = []
        self.client: BoseBluetoothClient | None = None
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.hotkey_manager = GlobalHotkey(self._on_global_hotkey)
        self.is_busy = False
        self.current_mode: str | None = None

        self.device_var = tk.StringVar()
        self.mac_var = tk.StringVar()
        self.channel_var = tk.StringVar(value=str(DEFAULT_QC35_RFCOMM_CHANNEL))
        self.backend_var = tk.StringVar(value="auto")
        self.status_var = tk.StringVar(value="Not connected")
        self.last_mode_var = tk.StringVar(value="Last mode sent: none")
        self.hotkey_var = tk.StringVar(value=self.config_data.get("hotkey", "Ctrl+Alt+N"))
        self.hotkey_status_var = tk.StringVar(value="Hotkey disabled")
        self.hotkey_on_start_var = tk.BooleanVar(value=bool(self.config_data.get("hotkey_on_start", False)))

        self._build_ui()
        self._set_anc_buttons_state("disabled")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_worker_queue()
        self.after(250, self.refresh_devices)
        if self.hotkey_on_start_var.get():
            self.after(600, self.enable_hotkey)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        header = ttk.Frame(self, padding=(16, 14, 16, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text="Bose QC35 II ANC Control", font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))

        device_frame = ttk.LabelFrame(self, text="Device", padding=12)
        device_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=8)
        device_frame.columnconfigure(1, weight=1)

        ttk.Label(device_frame, text="Detected").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.device_combo = ttk.Combobox(device_frame, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=0, column=1, sticky="ew")
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        self.refresh_button = ttk.Button(device_frame, text="Refresh", command=self.refresh_devices)
        self.refresh_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

        ttk.Label(device_frame, text="MAC").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Entry(device_frame, textvariable=self.mac_var).grid(row=1, column=1, sticky="ew", pady=(10, 0))

        ttk.Label(device_frame, text="Channel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Spinbox(device_frame, from_=1, to=30, textvariable=self.channel_var, width=8).grid(
            row=2,
            column=1,
            sticky="w",
            pady=(10, 0),
        )

        ttk.Label(device_frame, text="Backend").grid(row=2, column=1, sticky="e", padx=(0, 120), pady=(10, 0))
        self.backend_combo = ttk.Combobox(
            device_frame,
            textvariable=self.backend_var,
            state="readonly",
            values=available_backends(),
            width=16,
        )
        self.backend_combo.grid(row=2, column=1, sticky="e", pady=(10, 0))

        action_frame = ttk.Frame(device_frame)
        action_frame.grid(row=1, column=2, rowspan=2, sticky="e", padx=(8, 0), pady=(10, 0))
        self.connect_button = ttk.Button(action_frame, text="Connect", command=self.connect_device)
        self.connect_button.grid(row=0, column=0, padx=(0, 6))
        self.disconnect_button = ttk.Button(action_frame, text="Disconnect", command=self.disconnect_device)
        self.disconnect_button.grid(row=0, column=1)

        anc_frame = ttk.LabelFrame(self, text="Noise Cancellation", padding=12)
        anc_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=8)
        anc_frame.columnconfigure((0, 1, 2), weight=1, uniform="anc")

        self.high_button = ttk.Button(anc_frame, text="ANC High", command=lambda: self.set_anc_mode("high"))
        self.high_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.low_button = ttk.Button(anc_frame, text="ANC Low", command=lambda: self.set_anc_mode("low"))
        self.low_button.grid(row=0, column=1, sticky="ew", padx=6)
        self.off_button = ttk.Button(anc_frame, text="ANC Off", command=lambda: self.set_anc_mode("off"))
        self.off_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))

        ttk.Label(anc_frame, textvariable=self.last_mode_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))

        hotkey_frame = ttk.LabelFrame(self, text="Hotkey", padding=12)
        hotkey_frame.grid(row=3, column=0, sticky="ew", padx=16, pady=8)
        hotkey_frame.columnconfigure(1, weight=1)

        ttk.Label(hotkey_frame, text="Cycle").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(hotkey_frame, textvariable=self.hotkey_var, width=18).grid(row=0, column=1, sticky="w")
        ttk.Button(hotkey_frame, text="Enable", command=self.enable_hotkey).grid(row=0, column=2, sticky="e", padx=(8, 0))
        ttk.Button(hotkey_frame, text="Disable", command=self.disable_hotkey).grid(row=0, column=3, sticky="e", padx=(8, 0))
        ttk.Checkbutton(
            hotkey_frame,
            text="Enable on launch",
            variable=self.hotkey_on_start_var,
            command=self._save_config,
        ).grid(row=0, column=4, sticky="e", padx=(12, 0))
        ttk.Label(hotkey_frame, textvariable=self.hotkey_status_var).grid(
            row=1,
            column=0,
            columnspan=5,
            sticky="w",
            pady=(8, 0),
        )

        tools_frame = ttk.Frame(self, padding=(16, 0, 16, 8))
        tools_frame.grid(row=4, column=0, sticky="ew")
        ttk.Button(tools_frame, text="Read ANC", command=self.read_anc_mode).grid(row=0, column=0, sticky="w")
        ttk.Button(tools_frame, text="Environment", command=self.show_environment).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(tools_frame, text="Clear Log", command=self.clear_log).grid(row=0, column=2, sticky="w", padx=(8, 0))

        log_frame = ttk.LabelFrame(self, text="Debug Log", padding=8)
        log_frame.grid(row=5, column=0, sticky="nsew", padx=16, pady=(0, 16))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def refresh_devices(self) -> None:
        self._start_worker("refresh", self._refresh_devices_worker)

    def connect_device(self) -> None:
        try:
            address = self._selected_address()
            channel = self._selected_channel()
            backend = self.backend_var.get() or "auto"
        except Exception as exc:
            self.status_var.set("Invalid connection settings")
            self._append_log(f"ERROR {exc}")
            messagebox.showerror("Bose QC35 II ANC Control", str(exc))
            return

        self._start_worker("connect", lambda: self._connect_worker(address, channel, backend))

    def disconnect_device(self) -> None:
        if self.client:
            self.client.close()
            self.client = None
        self.status_var.set("Not connected")
        self._set_anc_buttons_state("disabled")

    def set_anc_mode(self, mode: str) -> None:
        self._start_worker(f"set_anc:{mode}", lambda: self._set_anc_worker(mode))

    def read_anc_mode(self) -> None:
        self._start_worker("read_anc", self._read_anc_worker)

    def show_environment(self) -> None:
        messagebox.showinfo("Environment", "\n".join(environment_summary()))

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def enable_hotkey(self) -> None:
        try:
            label = self.hotkey_manager.start(self.hotkey_var.get())
        except Exception as exc:
            self.hotkey_status_var.set("Hotkey disabled")
            self._append_log(f"HOTKEY ERROR {exc}")
            messagebox.showerror("Bose QC35 II ANC Control", str(exc))
            return

        self.hotkey_var.set(label)
        self.hotkey_status_var.set(f"Hotkey enabled: {label} cycles High -> Low -> Off")
        self._append_log(f"HOTKEY enabled: {label}")
        self._save_config()

    def disable_hotkey(self) -> None:
        self.hotkey_manager.stop()
        self.hotkey_status_var.set("Hotkey disabled")
        self._append_log("HOTKEY disabled")
        self._save_config()

    def on_close(self) -> None:
        self.hotkey_manager.stop()
        self.disconnect_device()
        self.destroy()

    def _refresh_devices_worker(self) -> None:
        self._queue_log("Searching Windows paired Bluetooth devices and optional PyBluez scan")
        devices = discover_bose_devices(scan_nearby=True)
        if not devices:
            self._queue_log("No Bose device found in Windows PnP list. You can enter the MAC manually.")
        self.worker_queue.put(("devices", devices))

    def _connect_worker(self, address: str, channel: int, backend: str) -> None:
        client = BoseBluetoothClient(
            address=address,
            channel=channel,
            backend=backend,
            log_callback=self._queue_log,
        )
        client.connect()
        responses = bose_protocol.initialize_qc35(client)
        for response in responses:
            self._queue_log(f"INIT {bose_protocol.format_response(response)}")
        self.worker_queue.put(("connected", client))

    def _set_anc_worker(self, mode: str) -> None:
        client = self._require_client()
        result = bose_protocol.set_anc(client, mode)
        for response in result.responses:
            self._queue_log(bose_protocol.format_response(response))
        self.worker_queue.put(("mode", result.reported_mode or result.requested_mode))

    def _read_anc_worker(self) -> None:
        client = self._require_client()
        mode = bose_protocol.read_anc(client)
        self.worker_queue.put(("mode", mode or "unknown"))

    def _on_global_hotkey(self) -> None:
        self.worker_queue.put(("hotkey", None))

    def _require_client(self) -> BoseBluetoothClient:
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Connect to the headphones first")
        return self.client

    def _selected_address(self) -> str:
        value = self.mac_var.get().strip()
        if not value:
            raise ValueError("Select a detected Bose device or enter its Bluetooth MAC")
        return format_mac(value)

    def _selected_channel(self) -> int:
        try:
            channel = int(self.channel_var.get())
        except ValueError as exc:
            raise ValueError("RFCOMM channel must be a number from 1 to 30") from exc
        if not 1 <= channel <= 30:
            raise ValueError("RFCOMM channel must be from 1 to 30")
        return channel

    def _on_device_selected(self, _event=None) -> None:
        index = self.device_combo.current()
        if 0 <= index < len(self.devices):
            device = self.devices[index]
            if device.address:
                self.mac_var.set(device.address)

    def _set_anc_buttons_state(self, state: str) -> None:
        for button in (self.high_button, self.low_button, self.off_button):
            button.configure(state=state)

    def _start_worker(self, name: str, target) -> None:
        if self.is_busy:
            self._append_log(f"BUSY ignoring {name}")
            return
        self.status_var.set(f"Working: {name}")
        self._set_controls_busy(True)

        def runner() -> None:
            try:
                target()
                self.worker_queue.put(("done", name))
            except Exception as exc:
                LOGGER.exception("Worker failed: %s", name)
                self.worker_queue.put(("error", exc))

        threading.Thread(target=runner, daemon=True).start()

    def _set_controls_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        self.refresh_button.configure(state=state)
        self.connect_button.configure(state=state)
        self.disconnect_button.configure(state=state)
        if not busy and self.client and self.client.is_connected:
            self._set_anc_buttons_state("normal")
        else:
            self._set_anc_buttons_state("disabled")

    def _poll_worker_queue(self) -> None:
        while True:
            try:
                kind, payload = self.worker_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(str(payload))
            elif kind == "devices":
                self._apply_devices(payload)
            elif kind == "connected":
                self.client = payload
                backend = self.client.backend_name or self.backend_var.get()
                self.status_var.set(f"Connected: {self.client.address} on channel {self.client.channel} ({backend})")
                self._set_anc_buttons_state("normal")
            elif kind == "mode":
                self.current_mode = str(payload).lower()
                self.last_mode_var.set(f"Last mode sent: {payload}")
                self.status_var.set(f"ANC mode: {payload}")
            elif kind == "hotkey":
                self._handle_hotkey_press()
            elif kind == "error":
                self.status_var.set("Error")
                self._append_log(f"ERROR {payload}")
                self._set_controls_busy(False)
                messagebox.showerror("Bose QC35 II ANC Control", str(payload))
            elif kind == "done":
                if self.client and self.client.is_connected:
                    self.status_var.set(self.status_var.get())
                self._set_controls_busy(False)

        self.after(100, self._poll_worker_queue)

    def _apply_devices(self, devices: list[BluetoothDevice]) -> None:
        self.devices = devices
        self.device_combo["values"] = [device.label() for device in devices]
        if devices:
            self.device_combo.current(0)
            if devices[0].address:
                self.mac_var.set(devices[0].address)
            self.status_var.set(f"Found {len(devices)} possible Bose device(s)")
        else:
            self.status_var.set("No Bose device detected. Enter MAC manually.")

    def _queue_log(self, message: str) -> None:
        self.worker_queue.put(("log", message))

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _handle_hotkey_press(self) -> None:
        label = self.hotkey_manager.label or self.hotkey_var.get()
        if self.is_busy:
            self._append_log(f"HOTKEY {label}: ignored while busy")
            return
        if not self.client or not self.client.is_connected:
            self._append_log(f"HOTKEY {label}: connect to the headphones first")
            self.status_var.set("Hotkey pressed, but headphones are not connected")
            return
        next_mode = self._next_cycle_mode()
        self._append_log(f"HOTKEY {label}: switching to {next_mode}")
        self.set_anc_mode(next_mode)

    def _next_cycle_mode(self) -> str:
        if self.current_mode in ANC_CYCLE:
            index = ANC_CYCLE.index(self.current_mode)
            return ANC_CYCLE[(index + 1) % len(ANC_CYCLE)]
        return ANC_CYCLE[0]

    def _load_config(self) -> dict:
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        data = {
            "hotkey": self.hotkey_var.get().strip() or "Ctrl+Alt+N",
            "hotkey_on_start": bool(self.hotkey_on_start_var.get()),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            self._append_log(f"CONFIG ERROR {exc}")


def run_app() -> None:
    app = BoseAncApp()
    app.mainloop()
