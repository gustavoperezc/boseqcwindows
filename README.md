# Bose QC35 II ANC Control for Windows

Interface screenshot (/userinterface.png)

Small Python/Tkinter proof of concept to control Bose QuietComfort 35 / 35 II
noise cancellation from Windows over Bluetooth RFCOMM, without the phone app.

## To execute
Opens with `python main.py` in the folder where you dumped the files.

## What works

* Detects likely paired Bose devices from Windows Bluetooth PnP entries.
* Lets you enter a Bluetooth MAC manually if discovery cannot extract it.
* Connects over RFCOMM, default channel `8` for QC35 / QC35 II.
* Sends Bose BMAP ANR commands:

  * High: `01 06 02 01 01`
  * Low: `01 06 02 01 03`
  * Off: `01 06 02 01 00`
* Adds a configurable global Windows hotkey to cycle:
  `High -> Low -> Off -> High`.
* Logs every packet sent and received in the UI and in `logs/bose_qc35_anc.log`.

## Important limitations

This is a technical proof of concept created by Gustavo Pérez, not an official Bose app. The command path implemented here
is based on current public reverse engineering for QC35/QC35 II:

* BMAP packet format: `[fblock, function, flags, payload_length, payload...]`
* QC35 ANR control: `[1.6]` with SETGET operator `2`
* ANR values: `0=off`, `1=high`, `2=wind`, `3=low`
* QC35 RFCOMM channel: `8`
* QC35 init packet before commands: GET `[0.1]`, hex `00 01 01 00`

If your headset firmware does not respond, try:

1. Make sure the non-LE Bose device is paired in Windows.
2. Power the headphones on and connect them as an audio device first.
3. Keep the default channel `8`, then test channels `1` to `30` if needed.

## Install

Use Python 3.10+ on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

There are no required pip packages. Tkinter is included in the standard Windows
Python installer.

Optional fallback:

```powershell
python -m pip install pybluez2
```

`pybluez2` has limited wheel coverage on newer Python versions. The app first
tries the native Windows Winsock backend via `ctypes`, so pybluez2 is only a
fallback.

## Run

```powershell
python main.py
```

Steps:

1. Click `Refresh`.
2. Select the detected Bose device, or enter the MAC manually.
3. Leave channel as `8`.
4. Click `Connect`.
5. Use `ANC High`, `ANC Low`, or `ANC Off`.

## Hotkey

The default hotkey is:

```text
Ctrl+Alt+N
```

In the app:

1. Connect to the headphones.
2. In `Hotkey`, keep `Ctrl+Alt+N` or enter another combination.
3. Click `Enable`.
4. Press the hotkey from any app to cycle `High -> Low -> Off`.

Supported keys include `A-Z`, `0-9`, `F1-F24`, `Space`, `Tab`, `Esc`, and arrow
keys. Use at least one modifier: `Ctrl`, `Alt`, `Shift`, or `Win`.

The hotkey only works while this app is running. If Windows reports that the
hotkey cannot be registered, another app probably already owns it.

## Troubleshooting

* `No Bose device detected`: enter the MAC manually. Windows PnP names do not
  always include the MAC.
* `WSA error 10060`: timeout. The device may be off, not connected, or the
  channel may be wrong.
* `WSA error 10049`: invalid address format or Windows cannot route to that
  Bluetooth address.
* `FunctionNotSupported` or `OperatorNotSupported`: firmware/model mismatch, or
  the packet reached a device that is not QC35/QC35 II.
* `OperatorNotSupportedOrAuthRequired`: the command path is not accepted by that
  firmware. The known QC35 ANR path uses SETGET, which should not require auth.

## Project structure

* `main.py`: app entry point and logging setup.
* `app_gui.py`: Tkinter interface and worker threads.
* `bluetooth_client.py`: Windows RFCOMM transport and device discovery.
* `bose_protocol.py`: BMAP packet building/parsing and ANR commands.
* `hotkey_manager.py`: native Windows global hotkey registration.
* `tests/test_bose_protocol.py`: protocol-level checks that do not require
  headphones.

## Author

Created by Gustavo Perez as an independent technical proof of concept for Bose QC35 / QC35 II users on Windows.

## References

* Microsoft SOCKADDR_BTH: https://learn.microsoft.com/windows/win32/api/ws2bth/ns-ws2bth-sockaddr_bth
* Microsoft RFCOMM overview: https://learn.microsoft.com/windows/apps/develop/devices-sensors/send-or-receive-files-with-rfcomm
* PyBluez docs: https://pybluez.readthedocs.io/en/latest/api/bluetooth_socket.html
* bosectl: https://github.com/aaronsb/bosectl
* Personal website: https://gustdev.com/

