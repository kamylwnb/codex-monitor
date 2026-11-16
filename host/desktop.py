"""DR. MOSFET Codex Monitor — native Windows companion."""
import asyncio
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import webbrowser
import tkinter as tk
from tkinter import filedialog, ttk
from types import SimpleNamespace

import meter

BG = "#10151d"
CARD = "#19212d"
TEXT = "#edf2f7"
MUTED = "#8c9bad"
TEAL = "#58dbb0"
ORANGE = "#ffa457"
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "branding"
SETTINGS = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DrMosfet/CodexMonitor/settings.json"

def find_codex():
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found and not found.lower().endswith((".cmd", ".bat")):
        return found
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation"],
                capture_output=True, text=True, timeout=12,
                creationflags=subprocess.CREATE_NO_WINDOW)
            for line in result.stdout.splitlines():
                path = Path(line.strip()) / "app/resources/codex.exe"
                if path.is_file():
                    return str(path)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""

def duration(seconds):
    seconds = max(0, int(seconds))
    days, hours = divmod(seconds // 3600, 24)
    minutes = seconds // 60 % 60
    if days:
        return f"{days} dni {hours} godz."
    if hours:
        return f"{hours} godz. {minutes:02d} min"
    return f"{minutes} min" if minutes else "mniej niż minutę"

def official_login_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host in ("auth.openai.com", "chatgpt.com") or
                                          host.endswith(".openai.com") or host.endswith(".chatgpt.com"))

class Desktop:
    def __init__(self, root):
        self.root = root
        root.title("Codex Monitor · DR. MOSFET")
        root.geometry("850x680")
        root.minsize(850, 680)
        root.configure(bg=BG)
        try:
            root.iconbitmap(str(ASSETS / "app.ico"))
        except tk.TclError:
            pass
        self.events = queue.Queue()
        self.thread = None
        self.loop = None
        self.task = None
        self.closing = False
        self.snapshot = None
        self.last_log = "Gotowy do połączenia."
        self.logs = []
        self.account_status = "Oczekiwanie na odczyt"
        self.link_status = "Rozłączono"
        self.bridge_refresh = None
        self.poll_refresh = None
        self.codex_path = ""
        self.login_requested = False
        self.transport = tk.StringVar(value="Bluetooth")
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
            self.codex_path = settings.get("codex", "")
            if settings.get("transport") in ("Bluetooth", "USB"):
                self.transport.set(settings["transport"])
        except (OSError, ValueError):
            pass
        self.make_ui()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.drain)
        root.after(1000, self.tick)
        root.after(350, self.start)

    def label(self, parent, text, size=11, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, action, accent=False):
        return tk.Button(parent, text=text, command=action, relief="flat", bd=0,
                         bg=TEAL if accent else "#293444", fg=BG if accent else TEXT,
                         activebackground="#7de8c6" if accent else "#38485e",
                         activeforeground=BG if accent else TEXT,
                         disabledforeground="#718096", cursor="hand2",
                         font=("Segoe UI", 10, "bold"), padx=18, pady=9)

    def make_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=30, pady=(26, 20))
        try:
            self.logo = tk.PhotoImage(file=str(ASSETS / "app-mark.png"))
            tk.Label(header, image=self.logo, bg=BG).pack(side="left", padx=(0, 16))
        except tk.TclError:
            pass
        title = tk.Frame(header, bg=BG)
        title.pack(side="left")
        self.label(title, "D R .  M O S F E T", 10, ORANGE, True).pack(anchor="w")
        self.label(title, "Codex Monitor", 27, TEXT, True).pack(anchor="w")
        self.badge = self.label(header, "●  ŁĄCZENIE", 10, MUTED, True)
        self.badge.pack(side="right", anchor="n", pady=12)

        intro = tk.Frame(self.root, bg=BG)
        intro.pack(fill="x", padx=32, pady=(0, 14))
        self.label(intro, "Twoje limity. Zawsze pod ręką.", 12, MUTED).pack(side="left")
        self.updated = self.label(intro, "", 10, MUTED)
        self.updated.pack(side="right")
        cards = tk.Frame(self.root, bg=BG)
        cards.pack(fill="x", padx=30)
        self.gauges = []
        for i in range(2):
            canvas = tk.Canvas(cards, width=382, height=205, bg=CARD, highlightthickness=0)
            canvas.pack(side="left", fill="x", expand=True, padx=(0, 14) if i == 0 else (0, 0))
            self.gauges.append(canvas)
        self.draw_cards()

        control = tk.Frame(self.root, bg=CARD)
        control.pack(fill="x", padx=30, pady=18)
        row = tk.Frame(control, bg=CARD)
        row.pack(fill="x", padx=18, pady=(16, 12))
        self.label(row, "FLIPPER ZERO", 10, MUTED, True).pack(side="left")
        self.link = self.label(row, self.link_status, 10, MUTED)
        self.link.pack(side="right")
        row = tk.Frame(control, bg=CARD)
        row.pack(fill="x", padx=18, pady=(0, 16))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground="#293444", background="#293444",
                        foreground=TEXT, bordercolor="#293444", padding=8, arrowcolor=TEXT)
        self.mode = ttk.Combobox(row, textvariable=self.transport, values=("Bluetooth", "USB"),
                                 width=13, state="readonly", font=("Segoe UI", 10))
        self.mode.pack(side="left", padx=(0, 12))
        self.connect = self.button(row, "Połącz", self.start, True)
        self.connect.pack(side="left", padx=(0, 8))
        self.disconnect = self.button(row, "Rozłącz", self.stop)
        self.disconnect.pack(side="left")
        self.refresh = self.button(row, "Odśwież teraz", self.refresh_now)
        self.refresh.pack(side="right")

        status = tk.Frame(self.root, bg=BG)
        status.pack(fill="x", padx=32)
        self.status_label = self.label(status, self.last_log, 10, MUTED, wraplength=780, justify="left")
        self.status_label.pack(anchor="w")
        self.account_label = self.label(status, self.account_status, 10, MUTED, wraplength=780, justify="left")
        self.account_label.pack(anchor="w", pady=(5, 8))
        install_link = self.label(status, "Jak zainstalować Codex na tym komputerze?", 9, MUTED)
        install_link.configure(cursor="hand2")
        install_link.pack(anchor="w")
        install_link.bind("<Button-1>", lambda _: webbrowser.open("https://learn.chatgpt.com/docs/codex-cli"))
        footer = tk.Frame(self.root, bg=BG)
        footer.pack(side="bottom", fill="x", padx=30, pady=(10, 20))
        self.label(footer, "Lokalnie na Twoim PC · co 60 s", 9, MUTED).pack(side="left")
        self.button(footer, "Wskaż Codex.exe", self.choose_codex).pack(side="right")
        self.login_button = self.button(footer, "Zaloguj konto", self.login)
        self.login_button.pack(side="right", padx=8)

    def draw_cards(self):
        now = time.time()
        windows = self.snapshot["windows"] if self.snapshot else [None, None]
        for i, (c, window) in enumerate(zip(self.gauges, windows)):
            c.delete("all")
            width = max(360, c.winfo_width())
            title = "LIMIT SESJI" if i == 0 else "LIMIT TYGODNIOWY"
            if window:
                minutes = window["minutes"]
                span = f"{minutes // 1440} dni" if minutes and minutes % 1440 == 0 else f"{minutes // 60} godz." if minutes % 60 == 0 else f"{minutes} min"
            else:
                span = "—"
            def text(x, y, value, size=11, color=TEXT, bold=False, anchor="nw"):
                c.create_text(x, y, text=value, fill=color, anchor=anchor,
                              font=("Segoe UI", size, "bold" if bold else "normal"))
            text(20, 17, title, 10, MUTED, True)
            text(width - 20, 17, span, 10, MUTED, anchor="ne")
            value = 100 - window["used"] if window else None
            text(20, 41, f"{value}%" if value is not None else "—", 39, TEXT, True)
            text(width - 20, 83, "pozostało", 11, MUTED, anchor="se")
            c.create_rectangle(20, 113, width - 20, 121, fill="#2b3747", outline="")
            color = TEAL if value is None or value > 25 else ORANGE if value > 10 else "#ff7385"
            if value:
                c.create_rectangle(20, 113, 20 + (width - 40) * value / 100, 121, fill=color, outline="")
            if window:
                seconds = window["reset"] - now
                text(20, 139, "Reset za " + duration(seconds) if seconds > 0 else "Oczekiwanie na nowy odczyt", 11)
                exact = dt.datetime.fromtimestamp(window["reset"]).astimezone().strftime("%d.%m · %H:%M")
                text(20, 168, exact, 10, MUTED)
            else:
                text(20, 139, "Brak danych tego okna" if self.snapshot else "Pobieranie limitów…", 11, MUTED)
        if self.snapshot:
            age = int(now - self.snapshot["fetched"])
            self.updated.configure(text=f"Odczyt: {age} s temu" if age < 60 else f"Odczyt: {age // 60} min temu",
                                   fg=MUTED if age < 150 else ORANGE)

    def choose_codex(self):
        path = filedialog.askopenfilename(title="Wskaż codex.exe", filetypes=[("Codex", "*.exe")])
        if path:
            self.codex_path = path
            self.save_settings()
            self.account_label.configure(text="Zapisano ścieżkę. Rozłącz i połącz, aby jej użyć.")

    def save_settings(self):
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps({"codex": self.codex_path, "transport": self.transport.get()}), encoding="utf-8")
        except OSError:
            pass

    def login(self):
        self.login_button.configure(state="disabled")
        self.stop()
        def when_stopped():
            if self.closing:
                return
            if self.thread and self.thread.is_alive():
                self.root.after(100, when_stopped)
                return
            self.snapshot = None
            self.draw_cards()
            self.login_requested = True
            self.start()
        self.root.after(150, when_stopped)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.save_settings()
        selected = self.transport.get()
        do_login = self.login_requested
        self.login_requested = False
        self.connect.configure(state="disabled")
        self.disconnect.configure(state="normal")
        self.mode.configure(state="disabled")
        self.link.configure(text="Szukanie Flippera…", fg=ORANGE)
        self.badge.configure(text="●  ŁĄCZENIE", fg=ORANGE)
        self.thread = threading.Thread(target=self.run, args=(selected, self.codex_path, do_login), daemon=True)
        self.thread.start()

    def run(self, selected, executable, do_login=False):
        async def monitor():
            children = []
            try:
                executable_path = executable or await asyncio.to_thread(find_codex)
                if not executable_path:
                    raise RuntimeError("Nie znaleziono Codex.exe. Użyj przycisku „Wskaż Codex.exe”.")
                if do_login:
                    self.events.put(("account", "Dokończ logowanie OpenAI w przeglądarce…"))
                    async with meter.Codex(executable_path) as auth:
                        login = await auth.call("account/login/start", {"type": "chatgpt"})
                        url = login.get("authUrl", "")
                        if not official_login_url(url):
                            raise RuntimeError("Codex zwrócił nieobsługiwany adres logowania.")
                        opened = await asyncio.to_thread(webbrowser.open, url)
                        if not opened:
                            raise RuntimeError("Nie udało się otworzyć przeglądarki do logowania.")
                        await auth.wait_for_login(login["loginId"])
                    self.events.put(("login_done", ""))
                self.bridge_refresh = asyncio.Event()
                self.poll_refresh = asyncio.Event()
                args = SimpleNamespace(once=False, transport="ble" if selected == "Bluetooth" else "usb",
                                       codex=executable_path, address=None, bucket=None, interval=60,
                                       port=None, refresh_event=self.bridge_refresh)
                async def poll():
                    while True:
                        try:
                            async with meter.Codex(executable_path) as codex:
                                result = await codex.call("account/rateLimits/read")
                                self.events.put(("quota", meter.snapshot(result)))
                        except Exception as exc:
                            self.events.put(("account", str(exc)))
                        self.poll_refresh.clear()
                        try:
                            await asyncio.wait_for(self.poll_refresh.wait(), 60)
                        except asyncio.TimeoutError:
                            pass
                meter.log = lambda message: self.events.put(("log", message))
                children = [asyncio.create_task(meter.main(args)), asyncio.create_task(poll())]
                await asyncio.gather(*children)
            finally:
                for child in children:
                    child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(monitor())
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.events.put(("account", str(exc)))
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()
            self.events.put(("stopped", ""))

    def stop(self):
        if self.loop and self.task and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.disconnect.configure(state="disabled")
        self.link.configure(text="Rozłączanie…", fg=MUTED)

    def refresh_now(self):
        if self.loop and not self.loop.is_closed():
            for event in (self.bridge_refresh, self.poll_refresh):
                if event:
                    self.loop.call_soon_threadsafe(event.set)

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "quota":
                    self.snapshot = value
                    self.account_status = "Konto Codex · aktualne dane"
                    self.account_label.configure(text=self.account_status, fg=TEAL)
                    self.draw_cards()
                elif kind == "log":
                    self.last_log = value
                    self.logs.append(value)
                    self.logs = self.logs[-100:]
                    self.status_label.configure(text=value)
                    if "Flipper polaczony" in value or "USB polaczone" in value or "Wyslano odczyt" in value:
                        self.link.configure(text=f"{self.transport.get()} · Połączono", fg=TEAL)
                        self.badge.configure(text="●  POŁĄCZONO", fg=TEAL)
                    elif "przerwane" in value or "USB:" in value:
                        self.link.configure(text="Ponawianie połączenia…", fg=ORANGE)
                        self.badge.configure(text="●  ŁĄCZENIE", fg=ORANGE)
                elif kind == "account":
                    self.account_label.configure(text=value, fg=ORANGE)
                elif kind == "login_done":
                    self.login_button.configure(state="normal")
                elif kind == "stopped":
                    self.login_button.configure(state="normal")
                    self.connect.configure(state="normal")
                    self.disconnect.configure(state="disabled")
                    self.mode.configure(state="readonly")
                    self.link.configure(text="Rozłączono", fg=MUTED)
                    self.badge.configure(text="●  OFFLINE", fg=MUTED)
        except queue.Empty:
            pass
        if self.closing and (not self.thread or not self.thread.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.drain)

    def tick(self):
        self.draw_cards()
        self.root.after(1000, self.tick)

    def close(self):
        self.closing = True
        self.stop()
        self.root.title("Codex Monitor · Zamykanie…")

def main():
    mutex = None
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DrMosfet.CodexMonitor")
        ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DrMosfet.CodexMonitor")
        if ctypes.windll.kernel32.GetLastError() == 183:
            return
    root = tk.Tk()
    Desktop(root)
    root.mainloop()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))

if __name__ == "__main__":
    main()
