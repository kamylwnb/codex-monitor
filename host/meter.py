# /// script
# requires-python = ">=3.11"
# dependencies = ["bleak>=3.0,<4", "pyserial>=3.5,<4"]
# ///
"""Codex Meter: account quota -> portable protocol -> Flipper BLE."""
import argparse
import asyncio
import json
import math
import re
import shutil
import subprocess
import time

TX = "19ed82ae-ed21-4c9d-4145-228e61fe0000"
RX = "19ed82ae-ed21-4c9d-4145-228e62fe0000"
ADV = re.compile(r"^0000308[0-9a-f]-0000-1000-8000-00805f9b34fb$")

def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)

class Codex:
    def __init__(self, executable):
        self.executable = executable
        self.proc = None
        self.serial = 0

    async def __aenter__(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.executable, "app-server", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if __import__('os').name == 'nt' else {}))
        try:
            await self.call("initialize", {"clientInfo": {"name": "codex_meter", "version": "1.0.0"}})
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            await self.__aexit__()
            raise
        return self

    def send(self, value):
        self.proc.stdin.write((json.dumps(value) + "\n").encode())

    async def call(self, method, params=None):
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params or {}})
        async def receive():
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    raise RuntimeError("Codex app-server zakonczyl prace")
                msg = json.loads(raw)
                if msg.get("id") != request_id:
                    if "id" in msg and "method" in msg:
                        self.send({"id": msg["id"], "error": {"code": -32601, "message": "Unsupported"}})
                    continue
                if "error" in msg:
                    raise RuntimeError(f"Blad API Codex: {msg['error'].get('code', 'unknown')}; sprawdz logowanie codex login")
                return msg["result"]
        return await asyncio.wait_for(receive(), 25)

    async def __aexit__(self, *_):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()

    async def wait_for_login(self, login_id):
        async def receive():
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    raise RuntimeError("Proces logowania zostal zamkniety")
                message = json.loads(raw)
                if message.get("method") == "account/login/completed":
                    params = message.get("params", {})
                    if params.get("loginId") == login_id:
                        if not params.get("success"):
                            raise RuntimeError("Logowanie nie zostalo zakonczone")
                        return
        await asyncio.wait_for(receive(), 300)

def snapshot(result, bucket=None, now=None):
    now = time.time() if now is None else now
    buckets = result.get("rateLimitsByLimitId") or {}
    if bucket:
        if bucket not in buckets:
            raise ValueError("Brak wybranego limitu. Dostepne: " + ", ".join(buckets))
        limits = buckets[bucket]
    else:
        limits = buckets.get("codex") or result.get("rateLimits")
    if not limits:
        raise ValueError("Brak danych limitu: sprawdz logowanie kontem ChatGPT")
    def window(value):
        if not value or value.get("usedPercent") is None:
            return None
        used = float(value["usedPercent"])
        duration = value.get("windowDurationMins")
        reset = value.get("resetsAt")
        if (not math.isfinite(used) or duration is None or reset is None
                or not math.isfinite(float(reset)) or not math.isfinite(float(duration))):
            return None
        return {"used": max(0, min(100, round(used))),
                "minutes": max(0, min(43200, int(duration))), "reset": float(reset)}
    return {"bucket": limits.get("limitId") or bucket or "codex", "fetched": now,
            "windows": [window(limits.get("primary")), window(limits.get("secondary"))]}

def frame(data, now=None):
    now = time.time() if now is None else now
    values = []
    for w in data["windows"]:
        values.extend([-1, 0, 0] if w is None else
                      [w["used"], max(0, min(2592000, int(w["reset"] - now))), w["minutes"]])
    return ("Q1 " + " ".join(map(str, values)) + "\n").encode("ascii")

def usb_command(port, payload):
    port.reset_input_buffer()
    port.write(b"\r\ncodex_meter " + payload.strip() + b"\r\n")
    port.flush()
    end = time.monotonic() + 4
    buffer = bytearray()
    while time.monotonic() < end:
        buffer.extend(port.read(max(1, port.in_waiting)))
        match = re.search(rb"METER (READY|R|OK|BAD)\r", buffer)
        if match:
            return match.group(1)
    raise RuntimeError("Brak odpowiedzi USB: otworz Codex Meter na Flipperze i zamknij qFlipper")

async def usb_main(args, executable):
    import serial
    from serial.tools import list_ports
    while True:
        ports = [p.device for p in list_ports.comports() if p.vid == 0x0483 and p.pid == 0x5740]
        selected = args.port or (ports[0] if len(ports) == 1 else None)
        if not selected:
            log("Podlacz Flipper przez USB. Przy kilku urzadzeniach podaj --port COM3.")
            await asyncio.sleep(5)
            continue
        try:
            with serial.Serial(selected, 230400, timeout=0.2, write_timeout=3) as port:
                await asyncio.sleep(.6)
                await asyncio.to_thread(usb_command, port, b"poll")
                log("USB polaczone: " + selected)
                async with Codex(executable) as codex:
                    last_fetch = 0
                    while True:
                        state = await asyncio.to_thread(usb_command, port, b"poll")
                        elapsed = time.monotonic() - last_fetch
                        manual = getattr(args, "refresh_event", None)
                        if elapsed >= args.interval or ((state == b"R" or (manual and manual.is_set())) and elapsed >= 5):
                            if manual:
                                manual.clear()
                            last_fetch = time.monotonic()
                            try:
                                data = snapshot(await codex.call("account/rateLimits/read"), args.bucket)
                                payload = frame(data)
                            except (RuntimeError, ValueError, asyncio.TimeoutError):
                                payload = b"E1\n"
                                log("Nie mozna pobrac limitu Codexa.")
                            await asyncio.to_thread(usb_command, port, payload)
                            log("Wyslano odczyt przez USB.")
                        await asyncio.sleep(1)
        except Exception as exc:
            log(f"USB: {type(exc).__name__}: {exc}")
            await asyncio.sleep(5)

async def serve(client, codex, args):
    hello = asyncio.Event()
    refresh = getattr(args, "refresh_event", None) or asyncio.Event()
    pending = bytearray()
    def notify(_, data):
        pending.extend(data)
        if len(pending) > 1024:
            pending.clear()
            return
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending[:] = rest
            if line.startswith(b"CODEXMETER 1"):
                hello.set()
            elif line == b"R":
                refresh.set()
    await client.start_notify(TX, notify)
    await asyncio.wait_for(hello.wait(), 8)
    log("Flipper polaczony. Otworzono Codex Meter.")
    last_fetch = 0
    while client.is_connected:
        delay = max(0, args.interval - (time.monotonic() - last_fetch))
        if delay:
            try:
                await asyncio.wait_for(refresh.wait(), delay)
            except asyncio.TimeoutError:
                pass
        refresh.clear()
        if not client.is_connected:
            return
        # Throttle repeated physical button presses.
        await asyncio.sleep(max(0, 5 - (time.monotonic() - last_fetch)))
        last_fetch = time.monotonic()
        try:
            data = snapshot(await codex.call("account/rateLimits/read"), args.bucket)
            payload = frame(data)
            log("Odswiezono limity: " + data["bucket"])
        except (RuntimeError, ValueError, asyncio.TimeoutError):
            payload = b"E1\n"
            log("Nie mozna odczytac limitu. Sprawdz codex login / dostepnosc sieci.")
        # 20 bytes works with the smallest BLE ATT MTU; receiver assembles lines.
        for i in range(0, len(payload), 20):
            await client.write_gatt_char(RX, payload[i:i + 20], response=True)

async def main(args):
    executable = args.codex or shutil.which("codex")
    if not executable:
        raise RuntimeError("Nie znaleziono Codex CLI. Zainstaluj je i wykonaj codex login.")
    if args.once:
        async with Codex(executable) as codex:
            result = await codex.call("account/rateLimits/read")
            data = snapshot(result, args.bucket)
            print(json.dumps(data, indent=2))
            print(frame(data).decode(), end="")
        return
    if args.transport == "usb":
        await usb_main(args, executable)
        return
    from bleak import BleakClient, BleakScanner
    def match(device, adv):
        return (device.address.lower() == args.address.lower()) if args.address else any(
            ADV.match(u.lower()) for u in adv.service_uuids)
    log("Otworz Codex Meter na Flipperze. Szukam Bluetooth...")
    while True:
        device = await BleakScanner.find_device_by_filter(match, timeout=10)
        if device is None:
            continue
        try:
            log("Znaleziono Flippera. Lacze BLE; zatwierdz kod parowania, jesli sie pojawi.")
            # Flipper changes GATT profiles between apps; avoid Windows' stale
            # HID/serial service cache after switching back to Codex Meter.
            async with BleakClient(device, pair=True, timeout=45,
                                   winrt={"use_cached_services": False}) as client:
                log("Polaczenie BLE gotowe, uruchamiam odczyt Codexa.")
                async with Codex(executable) as codex:
                    await serve(client, codex, args)
        except Exception as exc:
            log(f"Polaczenie przerwane ({type(exc).__name__}). Ponawiam za 5 s.")
            await asyncio.sleep(5)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Odczyt bez Bluetooth")
    parser.add_argument("--transport", choices=["ble", "usb"], default="ble")
    parser.add_argument("--port", help="Port USB, np. COM3")
    parser.add_argument("--codex", help="Sciezka do codex.exe")
    parser.add_argument("--address", help="Adres Bluetooth wybranego Flippera")
    parser.add_argument("--bucket", help="Identyfikator limitu, domyslnie codex")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    args.interval = max(15, args.interval)
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(str(exc))
        raise SystemExit(1)
