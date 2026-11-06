#!/usr/bin/env python3
"""
GUIDude - a lightweight Tk front end for AVRDUDE, in the spirit of AVRFuses.

Uses libavrdude directly through the SWIG module ``swig_avrdude`` (built
together with avrdude when SWIG and the Python headers are available).

Every action (read fuses, program flash, ...) runs in its own short
programmer session: open -> initialize -> check signature -> action -> close.
That keeps the GUI state simple and lets you re-plug the target or the
programmer at any time.

License: GPL-2.0-or-later (same as avrdude).
"""

import ctypes
import glob
import json
import os
import platform
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "GUIDude"
APP_VERSION = "0.9"


# ---------------------------------------------------------------------------
# Locating and loading libavrdude
# ---------------------------------------------------------------------------

def _here() -> Path:
    if getattr(sys, "frozen", False):           # PyInstaller
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def _lib_candidates():
    env = os.environ.get("GUIDUDE_LIBDIR")
    if env:
        yield Path(env)
    here = _here()
    yield here
    yield here / "lib"
    sysname = platform.system().lower()
    for b in (f"build_{sysname}", "build_msvc", "build_mingw64", "build"):
        yield here.parent / "avrdude" / b / "src"


def _find_libdir():
    for d in _lib_candidates():
        if (d / "swig_avrdude.py").is_file():
            return d
    return None


LIBDIR = _find_libdir()
if LIBDIR is not None:
    sys.path.insert(0, str(LIBDIR))
    if os.name == "nt":
        os.add_dll_directory(str(LIBDIR))

try:
    import swig_avrdude as ad
except ImportError as e:                        # pragma: no cover
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        APP_NAME,
        "Cannot load the libavrdude Python module (swig_avrdude).\n\n"
        "Build avrdude with SWIG support and run ./build_lib.sh, or set "
        f"GUIDUDE_LIBDIR to the directory containing swig_avrdude.py.\n\n{e}")
    sys.exit(1)


def find_config_file():
    cands = []
    if os.environ.get("GUIDUDE_CONF"):
        cands.append(Path(os.environ["GUIDUDE_CONF"]))
    for d in _lib_candidates():
        cands.append(d / "avrdude.conf")
    cands += [Path("/opt/homebrew/etc/avrdude.conf"),
              Path("/usr/local/etc/avrdude.conf"),
              Path("/etc/avrdude.conf")]
    for c in cands:
        if c.is_file():
            return c
    return None


def settings_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "guidude" / "settings.json"


def list_serial_ports():
    if os.name == "nt":
        ports = []
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DEVICEMAP\SERIALCOMM")
            i = 0
            while True:
                try:
                    ports.append(winreg.EnumValue(key, i)[1])
                    i += 1
                except OSError:
                    break
        except OSError:
            pass
        return sorted(ports)
    pats = ["/dev/cu.*"] if sys.platform == "darwin" else \
        ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS[0-3]", "/dev/serial/by-id/*"]
    res = []
    for p in pats:
        res += sorted(glob.glob(p))
    return [p for p in res if "Bluetooth" not in p and "debug-console" not in p]


# ---------------------------------------------------------------------------
# USB device detection (stdlib only: ioreg / sysfs / PowerShell)
# ---------------------------------------------------------------------------

class UsbDevice:
    """A connected USB device and the serial ports it provides."""
    __slots__ = ("vid", "pid", "product", "vendor", "serial", "ports")

    def __init__(self, vid, pid, product="", vendor="", serial="", ports=None):
        self.vid, self.pid = int(vid), int(pid)
        self.product, self.vendor = product or "", vendor or ""
        self.serial, self.ports = serial or "", list(ports or [])

    def key(self):
        return (self.vid, self.pid, self.serial, tuple(sorted(self.ports)))

    def ident(self):
        return f"{self.vid:04X}:{self.pid:04X}"

    def name(self):
        return self.product or self.vendor or "USB device"

    def describe(self, serial=True):
        extra = f", SN {self.serial}" if serial and self.serial else ""
        return f"{self.name()} ({self.ident()}{extra})"


# USB-to-UART bridge chips: such a device is first of all a serial port
# (Arduino boards, FTDI cables), even if avrdude also knows bit-bang
# programmers with the same VID/PID.
UART_BRIDGE_VIDS = {0x0403, 0x1A86, 0x10C4, 0x067B}


def _scan_usb_macos():
    import plistlib
    import subprocess
    out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-a"],
                         capture_output=True, timeout=5).stdout
    if not out.strip():
        return []
    devs = []

    def is_dev(e):
        return e.get("IOObjectClass") in ("IOUSBHostDevice", "IOUSBDevice") and "idVendor" in e

    def collect_ttys(e, acc):
        for c in e.get("IORegistryEntryChildren", []):
            if is_dev(c):              # a device behind a hub: separate entry
                visit(c)
                continue
            if "IOCalloutDevice" in c:
                acc.append(c["IOCalloutDevice"])
            collect_ttys(c, acc)

    def visit(e):
        ports = []
        collect_ttys(e, ports)
        devs.append(UsbDevice(
            e["idVendor"], e.get("idProduct", 0),
            e.get("USB Product Name") or e.get("kUSBProductString", ""),
            e.get("USB Vendor Name") or e.get("kUSBVendorString", ""),
            e.get("USB Serial Number") or e.get("kUSBSerialNumberString", ""),
            ports))

    for root in plistlib.loads(out):
        if is_dev(root):
            visit(root)
    return devs


def _scan_usb_linux():
    base = Path("/sys/bus/usb/devices")
    if not base.is_dir():
        return []

    def rd(p, n):
        try:
            return (p / n).read_text().strip()
        except OSError:
            return ""
    devs = {}
    for d in base.iterdir():
        v = rd(d, "idVendor")
        if v:
            devs[d.resolve()] = UsbDevice(int(v, 16), int(rd(d, "idProduct") or "0", 16),
                                          rd(d, "product"), rd(d, "manufacturer"),
                                          rd(d, "serial"))
    tb = Path("/sys/class/tty")
    if tb.is_dir():
        for t in tb.iterdir():
            dv = t / "device"
            if not dv.exists():
                continue
            p = dv.resolve()
            while p != p.parent and p not in devs:
                p = p.parent
            if p in devs:
                devs[p].ports.append("/dev/" + t.name)
    return list(devs.values())


def _scan_usb_windows():        # untested
    import subprocess
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match 'VID_[0-9A-F]{4}' }"
           " | ForEach-Object { $_.DeviceID + '|' + $_.Name }"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    devs, coms = {}, []
    for line in out.splitlines():
        if "|" not in line:
            continue
        did, name = line.split("|", 1)
        m = re.search(r"VID_([0-9A-F]{4})[&+]PID_([0-9A-F]{4})", did, re.I)
        if not m:
            continue
        vid, pid = int(m[1], 16), int(m[2], 16)
        c = re.search(r"\((COM\d+)\)", name)
        if c:
            coms.append((vid, pid, c[1]))
        if did.upper().startswith("USB\\") and "&MI_" not in did.upper():
            sn = did.split("\\")[-1]
            sn = "" if "&" in sn else sn
            devs.setdefault((vid, pid, sn), UsbDevice(vid, pid, name.strip(), "", sn))
    for vid, pid, port in coms:
        d = next((d for d in devs.values() if d.vid == vid and d.pid == pid), None)
        if d is None:
            d = devs.setdefault((vid, pid, ""), UsbDevice(vid, pid))
        d.ports.append(port)
    return list(devs.values())


def scan_usb():
    """List connected USB devices; never raises."""
    try:
        if sys.platform == "darwin":
            return _scan_usb_macos()
        if os.name == "nt":
            return _scan_usb_windows()
        return _scan_usb_linux()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Model helpers around libavrdude
# ---------------------------------------------------------------------------

def iter_list(lst, conv):
    n = ad.lfirst(lst)
    while n:
        yield conv(n)
        n = ad.lnext(n)


FUSE_RE = re.compile(r"^(lfuse|hfuse|efuse|fuse\d*)$")
LOCK_NAMES = ("lock", "lockbits")


class PartModel:
    """Everything the GUI needs to know about a part's fuses and lock bits."""

    def __init__(self, part):
        self.part = part
        self.desc = part.desc
        self.mems = {}                   # desc -> dict(size, initval, bitmask, title)
        self.fuse_mems, self.lock_mems = [], []
        for m in iter_list(part.mem, ad.ldata_avrmem):
            if FUSE_RE.match(m.desc) or m.desc in LOCK_NAMES:
                full = (1 << (8 * m.size)) - 1
                self.mems[m.desc] = dict(
                    size=m.size,
                    initval=None if m.initval == -1 else m.initval & full,
                    bitmask=full if m.bitmask == -1 else m.bitmask & full,
                    full=full,
                    title=ad.avr_mem_name(part, m))
                (self.lock_mems if m.desc in LOCK_NAMES else self.fuse_mems).append(m.desc)
        self.items = []                  # bit field descriptions
        for c in ad.get_config_table(part.desc) or []:
            m = ad.avr_locate_mem(part, c["memstr"])
            if m is None or m.desc not in self.mems:
                continue
            full = self.mems[m.desc]["full"]
            mask = c["mask"] & full
            if mask == 0:
                continue
            lsh = c["lsh"]
            vmax = mask >> lsh
            self.items.append(dict(
                name=c["name"], mem=m.desc, mask=mask, lsh=lsh,
                comment=c["ccomment"],
                values=[dict(value=v["value"] & vmax, label=v["label"],
                             comment=v["vcomment"]) for v in c["vlist"]]))
        pm = part.prog_modes
        self.modern = bool(pm & (ad.PM_UPDI | ad.PM_PDI))

    def defaults(self, mems):
        return {d: self.mems[d]["initval"] for d in mems}


def clock_kind(label: str):
    """Classify a clock-source label: 'clock' (external clock signal),
    'crystal' (crystal / resonator / external RC) or None (internal)."""
    l = label.lower()
    if l.startswith("extclk"):
        return "clock"
    if l.startswith(("ext", "xosc", "tosc", "fsosc", "cres")) or "xtal" in l:
        return "crystal"
    return None


def danger_messages(model: PartModel, values: dict, old: dict):
    """Return a list of warnings for risky settings in ``values``."""
    msgs = []
    for it in model.items:
        v = values.get(it["mem"])
        if v is None:
            continue
        fv = (v & it["mask"]) >> it["lsh"]
        sel = next((x for x in it["values"] if x["value"] == fv), None)
        lbl = sel["label"] if sel else ""
        name = it["name"]
        if name == "spien" and lbl == "isp_disabled":
            msgs.append("SPIEN: serial (ISP) programming will be DISABLED - "
                        "only high-voltage programming can recover the chip.")
        elif name == "dwen" and lbl == "dw_enabled":
            msgs.append("DWEN: debugWIRE will be enabled - ISP stops working until "
                        "debugWIRE is switched off with a debugWIRE-capable tool.")
        elif "warning" in lbl:
            msgs.append(f"{name.upper()}: {sel['comment']}")
        elif name == "rstpincfg" and any(x["label"] == "updi" for x in it["values"]) \
                and lbl != "updi":
            msgs.append("RSTPINCFG: UPDI pin will be reconfigured - only "
                        "high-voltage UPDI programming can recover the chip.")
        elif name in ("sut_cksel", "cksel") and clock_kind(lbl):
            # Reference: value read from the device, else the factory default
            ref = old.get(it["mem"])
            if ref is None:
                ref = model.mems[it["mem"]]["initval"]
            rsel = None
            if ref is not None:
                rfv = (ref & it["mask"]) >> it["lsh"]
                rsel = next((x for x in it["values"] if x["value"] == rfv), None)
            kind = clock_kind(lbl)
            if rsel is None or clock_kind(rsel["label"]) != kind:
                need = ("an external clock signal on the XTAL1/CLKI pin" if kind == "clock"
                        else "a crystal or resonator on the XTAL pins")
                msgs.append(f"Clock source: {sel['comment']}. The chip will need {need}; "
                            "without it, it stops running and cannot be programmed via "
                            "ISP until that clock is supplied.")
    for d in model.lock_mems:
        v, init = values.get(d), model.mems[d]["initval"]
        if v is not None and init is not None and \
                (v & model.mems[d]["bitmask"]) != (init & model.mems[d]["bitmask"]):
            msgs.append("Lock bits: memory access will be restricted; only a chip "
                        "erase (which also clears flash) removes the lock.")
    return msgs


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

class Tooltip:
    """Small delayed hover tip.

    Drawn as a label placed on top of the main window instead of a separate
    borderless toplevel: those look odd on macOS with Tk 9 (rounded frame,
    shadow, no native tooltip style) and behave differently on every
    platform. The tip is kept inside the window.
    """
    DELAY = 450

    def __init__(self, widget, text):
        self.widget, self.text, self.tip, self._after = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        if self.text:
            self._after = self.widget.after(self.DELAY, self.show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def show(self):
        self._after = None
        if self.tip or not self.text or not self.widget.winfo_ismapped():
            return
        top = self.widget.winfo_toplevel()
        self.tip = tip = tk.Label(top, text=self.text, justify="left",
                                  background="#ffffe1", foreground="#000000",
                                  font="TkTooltipFont", wraplength=380,
                                  padx=6, pady=3, borderwidth=1, relief="solid")
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        tw, th = top.winfo_width(), top.winfo_height()
        wx = self.widget.winfo_rootx() - top.winfo_rootx()
        wy = self.widget.winfo_rooty() - top.winfo_rooty()
        x = max(4, min(wx + 12, tw - w - 4))
        y = wy + self.widget.winfo_height() + 4
        if y + h > th - 4:
            y = max(4, wy - h - 4)
        tip.place(x=x, y=y)
        tip.lift()

    def hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class FilterCombobox(ttk.Combobox):
    """Editable combobox that narrows its list while typing."""

    def __init__(self, master, values=(), on_select=None, **kw):
        super().__init__(master, **kw)
        self.all_values = list(values)
        self.on_select = on_select
        self["values"] = self.all_values
        self._committed = ""
        self.bind("<KeyRelease>", self._filter)
        self.bind("<<ComboboxSelected>>", lambda e: self.commit())
        self.bind("<Return>", lambda e: self.commit())
        self.bind("<FocusOut>", lambda e: self.commit(silent_revert=True))

    def set_values(self, values):
        self.all_values = list(values)
        self["values"] = self.all_values

    def _filter(self, ev):
        if ev.keysym in ("Return", "Up", "Down", "Tab", "Escape"):
            return
        t = self.get().strip().lower()
        self["values"] = [v for v in self.all_values if t in v.lower()] or self.all_values

    def set_committed(self, text):
        self._committed = text
        self.set(text)

    def commit(self, silent_revert=False):
        t = self.get().strip()
        tl = t.lower()
        match = None
        if tl:
            for test in (lambda v: v == tl, lambda v: v.endswith(tl),
                         lambda v: v.startswith(tl), lambda v: tl in v):
                cands = [v for v in self.all_values if test(v.lower())]
                if len(cands) == 1:
                    match = cands[0]
                    break
                if len(cands) > 1:
                    break
        if match is None and self.on_select:
            match = self.on_select(t, probe=True)    # e.g. "m328p" -> "ATmega328P"
        if match is None:
            self.set(self._committed)
            self["values"] = self.all_values
            return
        self["values"] = self.all_values
        changed = match != self._committed
        self.set_committed(match)
        if changed and self.on_select:
            self.on_select(match)


class ScrollFrame(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, height=120)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._win, width=e.width))
        # scroll in pixels so that wheel and trackpad deltas map directly
        self.canvas.configure(yscrollcommand=self.vsb.set, yscrollincrement=1)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.bind_tree()

    def set_background(self, color):
        self.canvas.configure(background=color)

    # Mouse wheel / trackpad: bound directly to the canvas and to every
    # widget inside it (wheel events go to the widget under the pointer).
    # Instance bindings run before class bindings and return "break", so a
    # combobox or spinbox under the pointer scrolls the list instead of
    # changing its value.
    def bind_tree(self, w=None):
        if w is None:
            for x in (self.canvas, self.inner):
                self.bind_tree(x)
            return
        w.bind("<MouseWheel>", self._wheel)
        w.bind("<Shift-MouseWheel>", lambda e: "break")
        w.bind("<Button-4>", lambda e: self._scroll(-40))
        w.bind("<Button-5>", lambda e: self._scroll(40))
        try:                                    # Tk >= 8.7: trackpad events
            w.bind("<TouchpadScroll>", self._touchpad)
        except tk.TclError:
            pass
        for c in w.winfo_children():
            self.bind_tree(c)

    def _scroll(self, pixels):
        if self.canvas.yview() != (0.0, 1.0) and pixels:
            self.canvas.yview_scroll(int(pixels), "units")
        return "break"

    def _wheel(self, e):
        if tk.TkVersion >= 8.7:          # delta is 120 per notch on all platforms
            px = -e.delta / 4
        elif sys.platform == "darwin":   # Tk 8.6 on macOS: small integers
            px = -e.delta * 20
        else:                            # Tk 8.6 on Windows: 120 per notch
            px = -e.delta / 120 * 60
        return self._scroll(px)

    def _touchpad(self, e):
        try:
            _dx, dy = self.tk.splitlist(self.tk.call("tk::PreciseScrollDeltas", e.delta))
            return self._scroll(-float(dy))
        except (tk.TclError, ValueError):
            return "break"


# ---------------------------------------------------------------------------
# Fuse / lock bit editor (one instance for "Fuses", one for "Lock Bits")
# ---------------------------------------------------------------------------

class BitEditor(ttk.Frame):
    def __init__(self, master, app, kind):
        super().__init__(master, padding=6)
        self.app, self.kind = app, kind          # kind: "fuse" or "lock"
        self.model = None
        self.mems = []
        self.values = {}       # mem -> int (current editor state)
        self.read_vals = {}    # mem -> int (last value read from device)
        self.rows = []         # (item, var, widget, warnlabel)
        self.hexvars = {}
        self.hexentries = {}
        self._updating = False

        # Fixed parts first (pack order decides what survives shrinking),
        # the scrollable bit field list last.
        self.status = ttk.Label(self, text="", style="Dim.TLabel")
        self.status.pack(side="top", anchor="w")
        bb = ttk.Frame(self)
        bb.pack(side="bottom", fill="x", pady=(8, 0))
        self.hexbar = ttk.Frame(self)
        self.hexbar.pack(side="bottom", fill="x")
        self.sf = ScrollFrame(self)
        self.sf.pack(side="top", fill="both", expand=True, pady=(4, 6))

        what = "Fuses" if kind == "fuse" else "Lock Bits"
        self.btn_read = ttk.Button(bb, text=f"Read {what}", command=self.read)
        self.btn_verify = ttk.Button(bb, text="Verify", command=self.verify)
        self.btn_prog = ttk.Button(bb, text=f"Program {what}", command=self.program,
                                   style="Accent.TButton")
        self.btn_def = ttk.Button(bb, text="Factory Defaults", command=self.load_defaults)
        self.btn_def.pack(side="left")
        self.btn_prog.pack(side="right")
        self.btn_verify.pack(side="right", padx=6)
        self.btn_read.pack(side="right")
        app.action_buttons += [self.btn_read, self.btn_verify, self.btn_prog]

    # -- building -------------------------------------------------------
    def set_model(self, model: PartModel):
        self.model = model
        self.mems = model.fuse_mems if self.kind == "fuse" else model.lock_mems
        self.values = model.defaults(self.mems)
        self.read_vals = {}
        self._build()
        self._sync_from_values()
        self.status.configure(
            text="Showing factory defaults - press “Read” to load the "
                 "values from the device." if self.mems else
                 f"{model.desc} has no {'fuses' if self.kind == 'fuse' else 'lock bits'}.")

    def _build(self):
        inner = self.sf.inner
        for w in inner.winfo_children():
            w.destroy()
        for w in self.hexbar.winfo_children():
            w.destroy()
        self.rows, self.hexvars, self.hexentries = [], {}, {}
        m = self.model
        items = [it for it in m.items if it["mem"] in self.mems]
        if not items and self.mems:
            ttk.Label(inner, text="No bit field descriptions known for this part - "
                      "edit the hex values below.", style="Dim.TLabel").grid(
                row=0, column=0, sticky="w", padx=4, pady=8)
        inner.columnconfigure(2, weight=1)
        lastmem = None
        r = 0
        for it in items:
            if it["mem"] != lastmem and len(self.mems) > 1:
                hdr = ttk.Label(inner, text=m.mems[it["mem"]]["title"],
                                style="Section.TLabel")
                hdr.grid(row=r, column=0, columnspan=3, sticky="w",
                         pady=(10 if r else 0, 2), padx=2)
                r += 1
                lastmem = it["mem"]
            warn = ttk.Label(inner, text=" ", width=2, style="Warn.TLabel")
            warn.grid(row=r, column=0, sticky="w")
            warn.tip = Tooltip(warn, "")
            name = ttk.Label(inner, text=it["name"].upper(), style="Field.TLabel", width=12)
            name.grid(row=r, column=1, sticky="w", padx=(0, 6))
            Tooltip(name, f"{it['comment']}\nmask 0x{it['mask']:02X} in "
                          f"{m.mems[it['mem']]['title']}")
            vals = it["values"]
            single = (len(vals) == 2 and {v["value"] for v in vals} == {0, 1}
                      and bin(it["mask"]).count("1") == 1 and not m.modern)
            if single:
                var = tk.IntVar()
                w = ttk.Checkbutton(inner, variable=var,
                                    command=lambda it=it: self._ctrl_changed(it))
                w.grid(row=r, column=2, sticky="w", pady=1)
                kindw = "check"
            elif vals:
                var = tk.StringVar()
                w = ttk.Combobox(inner, textvariable=var, state="readonly",
                                 values=[v["comment"] for v in vals])
                w.grid(row=r, column=2, sticky="ew", pady=1)
                w.bind("<<ComboboxSelected>>", lambda e, it=it: self._ctrl_changed(it))
                kindw = "combo"
            else:
                var = tk.StringVar()
                vmax = it["mask"] >> it["lsh"]
                fr = ttk.Frame(inner)
                fr.grid(row=r, column=2, sticky="w", pady=1)
                w = ttk.Spinbox(fr, from_=0, to=vmax, textvariable=var, width=8,
                                command=lambda it=it: self._ctrl_changed(it))
                w.pack(side="left")
                w.bind("<Return>", lambda e, it=it: self._ctrl_changed(it))
                w.bind("<FocusOut>", lambda e, it=it: self._ctrl_changed(it))
                ttk.Label(fr, text=f"  {it['comment']} (0 … {vmax})",
                          style="Dim.TLabel").pack(side="left")
                kindw = "spin"
            self.rows.append((it, var, w, warn, kindw))
            r += 1
        self.sf.bind_tree()
        self.sf.canvas.after_idle(lambda: self.sf.canvas.yview_moveto(0))

        # hex value boxes, AVRFuses style
        for i, d in enumerate(self.mems):
            info = m.mems[d]
            f = ttk.Frame(self.hexbar)
            f.grid(row=i // 6, column=i % 6, sticky="w", padx=(0, 14), pady=(0, 4))
            short = info["title"].split("/")[-1].upper()
            hl = ttk.Label(f, text=short)
            hl.pack(side="top", anchor="w")
            Tooltip(hl, info["title"])
            v = tk.StringVar()
            e = ttk.Entry(f, textvariable=v, width=max(4, 2 * info["size"] + 2),
                          justify="center", font=self.app.mono)
            e.pack(side="top")
            e.bind("<Return>", lambda ev, d=d: self._hex_changed(d))
            e.bind("<FocusOut>", lambda ev, d=d: self._hex_changed(d))
            Tooltip(e, f"Value of {info['title']} in hex. Bits in use: "
                       f"0x{info['bitmask']:0{2*info['size']}X}"
                       + (f", factory default 0x{info['initval']:0{2*info['size']}X}"
                          if info["initval"] is not None else ""))
            self.hexvars[d], self.hexentries[d] = v, e
        state = "normal" if self.mems else "disabled"
        for b in (self.btn_read, self.btn_verify, self.btn_prog, self.btn_def):
            b.configure(state=state)

    # -- syncing ------------------------------------------------------------
    def _fmt(self, d, v):
        return "" if v is None else f"{v:0{2 * self.model.mems[d]['size']}X}"

    def _sync_from_values(self):
        self._updating = True
        try:
            for d in self.mems:
                self.hexvars[d].set(self._fmt(d, self.values.get(d)))
                self._mark_hex(d)
            for it, var, w, warn, kindw in self.rows:
                v = self.values.get(it["mem"])
                if v is None:
                    if kindw == "check":
                        var.set(0)
                        w.configure(text="(unknown)")
                    else:
                        var.set("")
                    continue
                fv = (v & it["mask"]) >> it["lsh"]
                sel = next((x for x in it["values"] if x["value"] == fv), None)
                if kindw == "check":
                    var.set(1 if fv == 0 else 0)          # checked = programmed (0)
                    w.configure(text=sel["comment"] if sel else f"value {fv}")
                elif kindw == "combo":
                    if sel:
                        w.configure(values=[x["comment"] for x in it["values"]])
                        var.set(sel["comment"])
                    else:
                        txt = f"0x{fv:X} - undefined / reserved value"
                        w.configure(values=[x["comment"] for x in it["values"]] + [txt])
                        var.set(txt)
                else:
                    var.set(str(fv))
            self._mark_dangers()
        finally:
            self._updating = False

    def _mark_hex(self, d):
        e = self.hexentries.get(d)
        if not e:
            return
        rv, cv = self.read_vals.get(d), self.values.get(d)
        mask = self.model.mems[d]["bitmask"]
        if rv is None or cv is None or (rv & mask) == (cv & mask):
            e.configure(style="TEntry")
        else:
            e.configure(style="Changed.TEntry")

    def _mark_dangers(self):
        msgs = danger_messages(self.model, self.values, self.read_vals)
        for it, var, w, warn, kindw in self.rows:
            n = it["name"].lower()
            mine = [m for m in msgs if m.lower().startswith(n + ":")
                    or (n in ("sut_cksel", "cksel") and m.startswith("Clock"))]
            # Lock bits: mark only the field that itself restricts access,
            # i.e. differs from the unlocked (factory) state
            d = it["mem"]
            if d in self.model.lock_mems:
                cur, init = self.values.get(d), self.model.mems[d]["initval"]
                if cur is not None and init is not None and \
                        (cur & it["mask"]) != (init & it["mask"]):
                    fv = (cur & it["mask"]) >> it["lsh"]
                    sel = next((x for x in it["values"] if x["value"] == fv), None)
                    mine.append(f"{sel['comment'] if sel else f'value {fv}'}: access "
                                "will be restricted; only a chip erase (which also "
                                "clears flash) removes the lock.")
            warn.configure(text="⚠" if mine else " ")
            warn.tip.text = "\n".join(mine)
        return msgs

    def _ctrl_changed(self, it):
        if self._updating or self.model is None:
            return
        for it2, var, w, warn, kindw in self.rows:
            if it2 is not it:
                continue
            if kindw == "check":
                fv = 0 if var.get() else 1
            elif kindw == "combo":
                sel = next((x for x in it["values"] if x["comment"] == var.get()), None)
                if sel is None:
                    return
                fv = sel["value"]
            else:
                try:
                    fv = int(var.get(), 0)
                except ValueError:
                    self._sync_from_values()
                    return
                fv = max(0, min(fv, it["mask"] >> it["lsh"]))
            d = it["mem"]
            cur = self.values.get(d)
            if cur is None:
                cur = self.model.mems[d]["initval"]
                if cur is None:
                    cur = self.model.mems[d]["full"]
            self.values[d] = (cur & ~it["mask"]) | ((fv << it["lsh"]) & it["mask"])
            break
        self._sync_from_values()

    def _hex_changed(self, d):
        if self._updating:
            return
        t = self.hexvars[d].get().strip().lower().removeprefix("0x")
        if t == "":
            self.values[d] = None
        else:
            try:
                v = int(t, 16)
                if v > self.model.mems[d]["full"]:
                    raise ValueError
                self.values[d] = v
            except ValueError:
                self.app.log(f"Invalid hex value '{self.hexvars[d].get()}' for "
                             f"{self.model.mems[d]['title']}", ad.MSG_WARNING)
        self._sync_from_values()

    def load_defaults(self):
        self.values = self.model.defaults(self.mems)
        self._sync_from_values()
        self.status.configure(text="Factory defaults loaded (not yet programmed).")

    # -- device actions -------------------------------------------------------
    def _read_mem(self, pgm, p, d):
        m = ad.avr_locate_mem(p, d)
        if ad.avr_read_mem(pgm, p, m) < 0:
            raise RuntimeError(f"failed to read {self.model.mems[d]['title']}")
        return int.from_bytes(m.get(m.size), "little")

    def read(self):
        def job(pgm, p):
            vals = {d: self._read_mem(pgm, p, d) for d in self.mems}
            return vals
        vals = self.app.session(job, f"Reading {self.kind}s")
        if vals is None:
            return
        self.values, self.read_vals = dict(vals), dict(vals)
        self._sync_from_values()
        s = ", ".join(f"{self.model.mems[d]['title']}=0x{self._fmt(d, v)}"
                      for d, v in vals.items())
        self.app.log(f"Read {s}", ad.MSG_INFO, tag="ok")
        self.status.configure(text=f"Values read from device at {time.strftime('%H:%M:%S')}.")

    def verify(self):
        if any(self.values.get(d) is None for d in self.mems):
            messagebox.showwarning(APP_NAME, "Some values are empty.")
            return
        def job(pgm, p):
            return {d: self._read_mem(pgm, p, d) for d in self.mems}
        got = self.app.session(job, "Verifying")
        if got is None:
            return
        self.read_vals = dict(got)
        bad = []
        for d in self.mems:
            mask = self.model.mems[d]["bitmask"]
            if (got[d] & mask) != (self.values[d] & mask):
                bad.append(f"{self.model.mems[d]['title']}: device 0x{self._fmt(d, got[d])}, "
                           f"expected 0x{self._fmt(d, self.values[d])}")
        self._sync_from_values()
        if bad:
            self.app.log("Verification FAILED: " + "; ".join(bad), ad.MSG_ERROR)
            messagebox.showerror(APP_NAME, "Verification failed:\n\n" + "\n".join(bad))
        else:
            self.app.log("Verification OK - device matches the values shown.", tag="ok")

    def program(self):
        if any(self.values.get(d) is None for d in self.mems):
            messagebox.showwarning(APP_NAME, "Some values are empty - read the device "
                                   "or load the factory defaults first.")
            return
        changes = []
        for d in self.mems:
            old = self.read_vals.get(d)
            new = self.values[d]
            t = self.model.mems[d]["title"]
            if old is None:
                changes.append(f"{t}:  0x{self._fmt(d, new)}")
            elif old != new:
                changes.append(f"{t}:  0x{self._fmt(d, old)} → 0x{self._fmt(d, new)}")
            else:
                changes.append(f"{t}:  0x{self._fmt(d, new)} (unchanged)")
        dangers = danger_messages(self.model, self.values, self.read_vals)
        msg = f"Program these values into {self.model.desc}?\n\n" + "\n".join(changes)
        if dangers:
            msg += "\n\n⚠ WARNING:\n\n" + "\n\n".join("• " + x for x in dangers)
        if not messagebox.askyesno(APP_NAME, msg,
                                   icon="warning" if dangers else "question",
                                   default="no" if dangers else "yes"):
            return

        def job(pgm, p):
            for d in self.mems:
                m = ad.avr_locate_mem(p, d)
                m.put(self.values[d].to_bytes(m.size, "little"))
                if ad.avr_write_mem(pgm, p, m, m.size) < 0:
                    raise RuntimeError(f"failed to write {self.model.mems[d]['title']}")
            return {d: self._read_mem(pgm, p, d) for d in self.mems}
        got = self.app.session(job, f"Programming {self.kind}s")
        if got is None:
            return
        self.read_vals = dict(got)
        self._sync_from_values()
        bad = [self.model.mems[d]["title"] for d in self.mems
               if (got[d] & self.model.mems[d]["bitmask"]) !=
                  (self.values[d] & self.model.mems[d]["bitmask"])]
        if bad:
            self.app.log("Programmed, but read-back differs for " + ", ".join(bad),
                         ad.MSG_ERROR)
            messagebox.showerror(APP_NAME, "Read-back after programming differs for:\n"
                                 + ", ".join(bad))
        else:
            self.app.log("Programming successful and verified.", tag="ok")
            self.status.configure(text=f"Programmed and verified at {time.strftime('%H:%M:%S')}.")


# ---------------------------------------------------------------------------
# Flash / EEPROM panel
# ---------------------------------------------------------------------------

FILETYPES = [("Intel Hex / S-Record / ELF / binary", "*.hex *.ihex *.eep *.srec *.s19 *.elf *.bin"),
             ("All files", "*")]


class MemoryPanel(ttk.Frame):
    def __init__(self, master, app, memname):
        super().__init__(master, padding=10)
        self.app, self.memname = app, memname
        self.columnconfigure(1, weight=1)
        title = "Flash" if memname == "flash" else "EEPROM"

        ttk.Label(self, text=f"{title} file:").grid(row=0, column=0, sticky="w")
        self.file = ttk.Combobox(self, values=app.settings.get(f"recent_{memname}", []))
        self.file.grid(row=0, column=1, sticky="ew", padx=6)
        last = app.settings.get(f"recent_{memname}", [])
        if last:
            self.file.set(last[0])
        bb_browse = ttk.Button(self, text="Browse…", command=self.browse)
        bb_browse.grid(row=0, column=2)
        Tooltip(bb_browse, f"Choose an existing file to program or verify.\n"
                f"To save the device contents to a (new) file use "
                f"\u201cRead {title} to File\u2026\u201d.")

        opts = ttk.Frame(self)
        opts.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        self.erase = tk.BooleanVar(value=app.settings.get(f"{memname}_erase", True))
        self.verify_after = tk.BooleanVar(value=app.settings.get(f"{memname}_verify", True))
        if memname == "flash":
            ttk.Checkbutton(opts, text="Erase chip before programming",
                            variable=self.erase).pack(anchor="w")
        ttk.Checkbutton(opts, text="Verify after programming",
                        variable=self.verify_after).pack(anchor="w")

        self.info = ttk.Label(self, text="", style="Dim.TLabel")
        self.info.grid(row=2, column=1, sticky="w", padx=6, pady=(10, 0))

        bb = ttk.Frame(self)
        bb.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        b_read = ttk.Button(bb, text=f"Read {title} to File…", command=self.read_to_file)
        b_ver = ttk.Button(bb, text="Verify", command=self.verify)
        b_prog = ttk.Button(bb, text=f"Program {title}", command=self.program,
                            style="Accent.TButton")
        b_prog.pack(side="right")
        b_ver.pack(side="right", padx=6)
        b_read.pack(side="left")
        Tooltip(b_read, f"Read the {title} contents from the device and save them to a new "
                f"or existing file (Intel Hex, S-Record or raw binary, by extension). "
                f"The file in the field above is suggested.")
        app.action_buttons += [b_read, b_ver, b_prog]

    def set_model(self, model):
        m = ad.avr_locate_mem(model.part, self.memname)
        if m is None:
            self.info.configure(text=f"{model.desc} has no {self.memname} memory.")
        else:
            self.info.configure(text=f"{model.desc}: {m.size} bytes {self.memname}"
                                + (f", page size {m.page_size}" if m.page_size > 1 else ""))

    def browse(self):
        cur = self.file.get()
        f = filedialog.askopenfilename(
            title=f"Select {self.memname} file", filetypes=FILETYPES,
            initialdir=os.path.dirname(cur) if cur else None)
        if f:
            self.file.set(f)
            self._remember(f)

    def _remember(self, f):
        lst = [f] + [x for x in self.app.settings.get(f"recent_{self.memname}", []) if x != f]
        self.app.settings[f"recent_{self.memname}"] = lst[:10]
        self.file["values"] = lst[:10]

    def _check_file(self):
        f = self.file.get().strip()
        if not f:
            self.browse()
            f = self.file.get().strip()
        if not f:
            return None
        if not os.path.isfile(f):
            messagebox.showerror(APP_NAME, f"File not found:\n{f}")
            return None
        self._remember(f)
        return f

    def _load(self, p, fname, allow_empty=False):
        n = ad.fileio(ad.FIO_READ, fname, ad.FMT_AUTO, p, self.memname, -1)
        if n < 0:
            raise RuntimeError(f"cannot read {fname}")
        if n == 0 and not allow_empty:
            raise RuntimeError(f"{os.path.basename(fname)} contains no {self.memname} "
                               "data - nothing to program")
        return n

    def _compare(self, pgm, p, m, n, expected):
        # Read back only the pages that are allocated by the file
        if ad.avr_read_mem(pgm, p, m, p) < 0:
            raise RuntimeError(f"failed to read {self.memname}")
        got = m.get(n)
        if got == expected:
            return None
        for i, (a, b) in enumerate(zip(got, expected)):
            if a != b:
                return i, a, b
        return n, 0, 0

    def program(self):
        f = self._check_file()
        if not f:
            return
        erase = self.memname == "flash" and self.erase.get()
        verify = self.verify_after.get()

        def job(pgm, p):
            m = ad.avr_locate_mem(p, self.memname)
            n = self._load(p, f)
            expected = m.get(n)
            if erase:
                self.app.log("Erasing chip", ad.MSG_INFO)
                if pgm.chip_erase(p) < 0:
                    raise RuntimeError("chip erase failed")
            rc = ad.avr_write_mem(pgm, p, m, n, not erase)
            if rc < 0:
                raise RuntimeError(f"failed to write {self.memname} (rc={rc})")
            self.app.log(f"Wrote {n} bytes to {self.memname}", tag="ok")
            if verify:
                return self._compare(pgm, p, m, n, expected), n
            return "noverify", n
        res = self.app.session(job, f"Programming {self.memname}")
        self._report(res)

    def verify(self):
        f = self._check_file()
        if not f:
            return

        def job(pgm, p):
            m = ad.avr_locate_mem(p, self.memname)
            n = self._load(p, f, allow_empty=True)
            if n == 0:
                # an empty file (e.g. read from an erased chip) means: all 0xFF
                last = ad.avr_read_mem(pgm, p, m)
                if last < 0:
                    raise RuntimeError(f"failed to read {self.memname}")
                if last == 0:
                    return None, "empty"
                data = m.get(last)
                i = next(i for i, b in enumerate(data) if b != 0xFF)
                return (i, data[i], 0xFF), "empty"
            expected = m.get(n)
            return self._compare(pgm, p, m, n, expected), n
        self._report(self.app.session(job, f"Verifying {self.memname}"))

    def _report(self, res):
        if res is None:
            return
        diff, n = res
        if diff == "noverify":
            return
        if diff is None and n == "empty":
            self.app.log(f"{self.memname} is empty (all 0xFF), as is the file - verified OK",
                         tag="ok")
        elif diff is None:
            self.app.log(f"{n} bytes of {self.memname} verified OK", tag="ok")
        else:
            a, dev, exp = diff
            self.app.log(f"Verification FAILED at 0x{a:04X}: device 0x{dev:02X}, "
                         f"file 0x{exp:02X}", ad.MSG_ERROR)
            messagebox.showerror(APP_NAME, f"{self.memname} verification failed at "
                                 f"address 0x{a:04X}\n(device 0x{dev:02X}, file 0x{exp:02X})")

    def read_to_file(self):
        # Suggest the file from the field (same folder and name); the save
        # dialog lets the user keep it (confirming the overwrite) or type a new name
        cur = self.file.get().strip()
        ext = ".hex" if self.memname == "flash" else ".eep"
        init = os.path.dirname(cur) if cur else None
        name = os.path.basename(cur) if cur else f"{self.app.model.part.id}_{self.memname}{ext}"
        f = filedialog.asksaveasfilename(
            title=f"Save {self.memname} contents", defaultextension=ext,
            initialdir=init, initialfile=name,
            filetypes=[("Intel Hex", "*.hex *.eep"), ("S-Record", "*.srec"),
                       ("Raw binary", "*.bin")])
        if not f:
            return
        low = f.lower()
        fmt = ad.FMT_SREC if low.endswith((".srec", ".s19")) else \
            ad.FMT_RBIN if low.endswith(".bin") else ad.FMT_IHEX

        def job(pgm, p):
            m = ad.avr_locate_mem(p, self.memname)
            n = ad.avr_read_mem(pgm, p, m)
            if n < 0:
                raise RuntimeError(f"failed to read {self.memname}")
            if n == 0:   # still write the (empty) file, like the avrdude CLI
                self.app.log(f"{self.memname} is empty (all 0xFF)", ad.MSG_INFO)
                n = m.size if fmt == ad.FMT_RBIN else 0
            if ad.fileio(ad.FIO_WRITE, f, fmt, p, self.memname, n) < 0:
                raise RuntimeError(f"cannot write {f}")
            return n
        n = self.app.session(job, f"Reading {self.memname}")
        if n is None:
            return
        self.app.log(f"Saved {n} bytes of {self.memname} to {f}"
                     + (" (memory empty)" if n == 0 else ""), tag="ok")
        self.file.set(f)                # now ready for Verify / Program
        self._remember(f)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

PORT_SEP = "  \u2014  "   # separates port and description in the port list
SCAN_INTERVAL = 2.5 if os.name != "nt" else 6.0   # seconds between USB scans
PGM_FILTERS = ("detected", "for device", "all")
# preferred programming interface when several programmers fit
PM_PRIORITY = ("PM_UPDI", "PM_ISP", "PM_PDI", "PM_TPI", "PM_JTAG", "PM_XMEGAJTAG",
               "PM_JTAGmkI", "PM_SPM", "PM_debugWIRE", "PM_HVSP", "PM_HVPP")
SERIAL_PREF = ("serialupdi", "arduino", "urclock", "jtag2updi", "stk500v2",
               "stk500v1", "avr109", "butterfly")


# Serial-port (CDC) programmers that avrdude cannot tell apart by USB IDs
# alone: preferred programmer type per (VID, PID); PID None = whole vendor.
SERIAL_HINTS = {
    (0x16C0, 0x2A9B): ("stk500v2",),            # Diamex ALL-AVR / AVR-ISP stick
    (0x1FFB, 0x0081): ("stk500v2",),            # Pololu USB AVR Programmer
    (0x1FFB, 0x00B0): ("stk500v2",),            # Pololu USB AVR Programmer v2.x
    (0x2341, None): ("arduino", "urclock"),     # Arduino boards
    (0x2A03, None): ("arduino", "urclock"),     # Arduino.org boards
}


def serial_pref_for(dev):
    """Order of preference for serial programmers on the port of ``dev``."""
    hint = None
    if dev is not None:
        hint = SERIAL_HINTS.get((dev.vid, dev.pid)) or SERIAL_HINTS.get((dev.vid, None))
        if hint is None and re.search(r"pirate", f"{dev.name()} {dev.serial}", re.I):
            hint = ("buspirate", "buspirate_bb")
        if hint is None and re.search(r"isp|stk ?500|programmer", dev.name(), re.I):
            hint = ("stk500v2", "stk500")       # an ISP programmer, not a board
    return tuple(hint or ()) + SERIAL_PREF


def pm_rank(pm):
    for i, name in enumerate(PM_PRIORITY):
        if pm & getattr(ad, name, 0):
            return i
    return len(PM_PRIORITY)


LOG_MIN = 84        # minimum height of the log pane (header + ~2 lines)

LOGLEVELS = [("Quiet", -1), ("Normal", 0), ("Verbose", 1), ("Very verbose", 2),
             ("Debug", 3), ("Trace", 4)]


class App:
    LOG_MIN = LOG_MIN
    SASH = 8

    def __init__(self, root):
        self.root = root
        self.busy = False
        self.action_buttons = []
        self.model = None
        self._last_update = 0.0
        self._prog_hdr = ""
        self.sp = settings_path()
        try:
            self.settings = json.loads(self.sp.read_text())
        except Exception:
            self.settings = {}

        root.title(APP_NAME)
        root.geometry(self.settings.get("geometry", "860x780"))
        root.protocol("WM_DELETE_WINDOW", self.quit)
        # In the app bundle there is no stderr: report unexpected Python errors
        # in callbacks in the GUI instead of losing them (or the program).
        root.report_callback_exception = self._callback_error
        self._styles()
        self._build()
        self._build_menu()

        # libavrdude
        ad.set_msg_callback(self.msg_callback)
        ad.set_progress_callback(self.progress_callback)
        ad.cvar.verbose = self.settings.get("verbose", 0)
        ad.init_cx()
        ad.init_config()
        conf = find_config_file()
        if conf is None or ad.read_config(str(conf)) != 0:
            messagebox.showerror(APP_NAME, "Cannot read avrdude.conf.\n\nSet GUIDUDE_CONF "
                                 "to its location.")
            sys.exit(1)
        self.log(f"{APP_NAME} {APP_VERSION} with libavrdude {ad.cvar.version}", ad.MSG_NOTICE)
        self.log(f"Configuration: {conf}", ad.MSG_NOTICE)
        rc = Path.home() / ".avrduderc"
        if rc.is_file():
            ad.read_config(str(rc))
            self.log(f"Also read {rc}", ad.MSG_NOTICE)
        self.usb_devs = scan_usb()
        self._scan_thread, self._scan_result, self._scan_started = None, None, time.monotonic()
        self._load_lists()
        for d in self._relevant(self.usb_devs):
            self.log(f"Detected {d.describe()}" + (f" on {', '.join(d.ports)}" if d.ports else ""),
                     tag="notice")
        self._auto_select("Detected hardware")
        self.root.after(500, self._scan_tick)

    # -- look & feel ---------------------------------------------------------
    def _styles(self):
        st = ttk.Style()
        # The mouse wheel must never silently change a combobox value (device,
        # fuse settings, ...) while the user just wants to scroll.
        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Mod2-MouseWheel>",
                    "<Shift-Mod2-MouseWheel>", "<TouchpadScroll>",
                    "<Button-4>", "<Button-5>"):
            try:
                self.root.bind_class("TCombobox", seq, "")
            except tk.TclError:
                pass
        if sys.platform not in ("darwin", "win32") and "clam" in st.theme_names():
            st.theme_use("clam")
        import tkinter.font as tkfont
        self.mono = tkfont.nametofont("TkFixedFont").copy()
        base = tkfont.nametofont("TkDefaultFont")
        small = base.copy()
        small.configure(size=max(9, base.cget("size") - 1))
        bold = base.copy()
        bold.configure(weight="bold")
        self.fonts = (small, bold)
        st.configure("Dim.TLabel", foreground="#808080", font=small)
        st.configure("Field.TLabel", font=self.mono)
        st.configure("Section.TLabel", font=bold)
        st.configure("Warn.TLabel", foreground="#d03020", font=bold)
        st.configure("Sig.TLabel", font=self.mono)
        st.configure("Changed.TEntry", foreground="#c05000", fieldbackground="#fff3d6")
        st.configure("Accent.TButton", font=bold)

    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(10, 8, 10, 4))
        top.pack(side="top", fill="x")
        top.columnconfigure(1, weight=1)
        top.columnconfigure(4, weight=0)

        ttk.Label(top, text="Device:").grid(row=0, column=0, sticky="w")
        df = ttk.Frame(top)
        df.grid(row=0, column=1, columnspan=2, sticky="ew", padx=6)
        self.dev_cb = FilterCombobox(df, on_select=self.device_selected, width=22)
        self.dev_cb.pack(side="left")
        Tooltip(self.dev_cb, "Type to search (name or avrdude id such as m328p)")
        self.btn_detect = ttk.Button(df, text="Detect", command=self.detect)
        self.btn_detect.pack(side="left", padx=6)
        Tooltip(self.btn_detect, "Read the signature and select the matching device")
        self.sig_lbl = ttk.Label(df, text="", style="Sig.TLabel")
        self.sig_lbl.pack(side="left", padx=(6, 0))

        ttk.Label(top, text="Programmer:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.pgm_cb = FilterCombobox(top, on_select=self.programmer_selected, width=30)
        self.pgm_cb.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        flt = self.settings.get("pgm_filter")
        if flt not in PGM_FILTERS:
            flt = "all" if self.settings.get("all_programmers") else "detected"
        self.filter_cb = ttk.Combobox(top, state="readonly", width=10, values=PGM_FILTERS)
        self.filter_cb.set(flt)
        self.filter_cb.grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.filter_cb.bind("<<ComboboxSelected>>", lambda e: self._filter_changed())
        Tooltip(self.filter_cb,
                "detected: programmers whose USB hardware is connected (and serial\n"
                "programmers if a USB serial port is present), narrowed by the port;\n"
                "for device: all programmers that support the selected device;\n"
                "all: every programmer avrdude knows")

        ttk.Label(top, text="Port:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        pf = ttk.Frame(top)
        pf.grid(row=2, column=1, columnspan=2, sticky="ew", padx=6, pady=(6, 0))
        self.port_cb = ttk.Combobox(pf, width=22, postcommand=self._fill_ports)
        self.port_cb.pack(side="left")
        self.port_cb.set(self.settings.get("port", "usb"))
        for ev in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.port_cb.bind(ev, self._port_changed, add="+")
        Tooltip(self.port_cb, "usb, usb:<serial number> for one of several identical USB "
                "programmers, or a serial port. Connected devices are listed with their names.")
        ttk.Label(pf, text="Bit clock (µs):").pack(side="left", padx=(14, 0))
        self.bitclock = ttk.Entry(pf, width=6)
        self.bitclock.pack(side="left", padx=4)
        self.bitclock.insert(0, self.settings.get("bitclock", ""))
        # width=1 + fill: the label only uses left-over space (never widens
        # the window); the full text is in its tooltip
        self.hw_lbl = ttk.Label(pf, text="", style="Dim.TLabel", width=1)
        self.hw_lbl.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.hw_tip = Tooltip(self.hw_lbl, "")
        Tooltip(self.bitclock, "Optional ISP bit clock period in µs (like avrdude -B). "
                "Use e.g. 32 for targets running from a slow clock. Leave empty for default.")

        # paned: notebook + log
        # tk (not ttk) PanedWindow: it supports a minimum size per pane, so the
        # sash cannot be dragged over the action buttons
        bg = ttk.Style().lookup("TFrame", "background") or None
        self.pw = pw = tk.PanedWindow(r, orient="vertical", sashwidth=self.SASH,
                                      borderwidth=0, sashrelief="flat",
                                      **({"background": bg} if bg else {}))
        self.nbf = nbf = ttk.Frame(pw)
        self.nb = ttk.Notebook(nbf)
        self.nb.pack(fill="both", expand=True)
        self.fuses = BitEditor(self.nb, self, "fuse")
        self.locks = BitEditor(self.nb, self, "lock")
        self.flash = MemoryPanel(self.nb, self, "flash")
        self.eeprom = MemoryPanel(self.nb, self, "eeprom")
        self.nb.add(self.fuses, text="Fuses")
        self.nb.add(self.locks, text="Lock Bits")
        self.nb.add(self.flash, text="Flash")
        self.nb.add(self.eeprom, text="EEPROM")
        self.nb.select(self.settings.get("tab", 0))
        pw.add(nbf, stretch="always")

        logf = ttk.Frame(pw)
        lh = ttk.Frame(logf)
        lh.pack(side="top", fill="x", pady=(4, 2))
        ttk.Label(lh, text="Log").pack(side="left")
        ttk.Button(lh, text="Clear", width=6, command=self.clear_log).pack(side="right")
        self.verb_cb = ttk.Combobox(lh, state="readonly", width=12,
                                    values=[n for n, _ in LOGLEVELS])
        lvl = self.settings.get("verbose", 0)
        self.verb_cb.set(next((n for n, v in LOGLEVELS if v == lvl), "Normal"))
        self.verb_cb.bind("<<ComboboxSelected>>", self._verbosity)
        self.verb_cb.pack(side="right", padx=6)
        lf = ttk.Frame(logf)
        lf.pack(side="top", fill="both", expand=True)
        self.logtxt = tk.Text(lf, height=5, wrap="word", font=self.mono,
                              state="disabled", borderwidth=1, relief="solid")
        sb = ttk.Scrollbar(lf, command=self.logtxt.yview)
        self.logtxt.configure(yscrollcommand=sb.set)
        self.logtxt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, col in (("error", "#d02020"), ("warning", "#c07000"), ("ok", "#109030"),
                         ("notice", "#6070a0"), ("debug", "#909090")):
            self.logtxt.tag_configure(tag, foreground=col)
        pw.add(logf, stretch="never", minsize=self.LOG_MIN)
        # The split between notebook and log is managed in _layout_panes():
        # the log keeps its preferred height (set by dragging the sash) but
        # is the first to give up space when the window gets smaller.
        self.log_pref = self.settings.get("log_height", 140)
        self._nb_min = 0
        pw.bind("<Configure>", self._layout_panes)
        pw.bind("<ButtonRelease-1>", self._sash_moved)

        # status bar
        sbar = ttk.Frame(r, padding=(10, 2, 10, 8))
        sbar.pack(side="bottom", fill="x")
        self.btn_erase = ttk.Button(sbar, text="Erase Chip…", command=self.erase_chip)
        self.btn_erase.pack(side="left")
        self.action_buttons += [self.btn_erase, self.btn_detect]
        self.status = ttk.Label(sbar, text="Ready")
        self.status.pack(side="left", padx=12)
        self.progress = ttk.Progressbar(sbar, length=200, maximum=100)
        self.progress.pack(side="right")
        # packed last so that shrinking the window squeezes the middle part,
        # never the toolbar or the status bar
        pw.pack(side="top", fill="both", expand=True, padx=10, pady=4)

    # -- lists ---------------------------------------------------------------
    def _load_lists(self):
        parts = []
        for p in iter_list(ad.cvar.part_list, ad.ldata_avrpart):
            if p.id.startswith(".") or not p.signature or p.prog_modes == 0:
                continue
            parts.append(p)
        self.parts = {p.desc: p for p in parts}
        self.part_ids = {p.id.lower(): p.desc for p in parts}
        self.dev_cb.set_values(sorted(self.parts, key=str.lower))

        self.pgms = {}
        for g in iter_list(ad.cvar.programmers, ad.ldata_programmer):
            ids = list(iter_list(g.id, ad.ldata_string))
            if not ids or ids[0].startswith(".") or not g.prog_modes:
                continue
            label = f"{ids[0]}  –  {g.desc}"
            pids = []
            n = ad.lfirst(g.usbpid)
            while n:
                pids.append(ctypes.c_int.from_address(int(ad.ldata(n))).value)
                n = ad.lnext(n)
            # (id, prog_modes, all ids, conntype, usb vid, usb pids)
            self.pgms[label] = (ids[0], g.prog_modes, ids, g.conntype, g.usbvid, pids)

        dev = self.settings.get("device", "ATmega328P")
        if dev not in self.parts:
            dev = "ATmega328P" if "ATmega328P" in self.parts else next(iter(self.parts))
        self.dev_cb.set_committed(dev)
        self.device_selected(dev)

    # -- programmer / port / connected hardware ---------------------------------
    def current_port(self):
        return self.port_cb.get().split(PORT_SEP)[0].strip()

    def _matches(self, info, dev):
        """Does USB device ``dev`` look like the hardware of programmer ``info``?"""
        vid, pids = info[4], info[5]
        if info[3] != ad.CONNTYPE_USB or not vid or vid != dev.vid:
            return False
        if pids:
            return dev.pid in pids
        # e.g. FLIP DFU bootloaders: the PID belongs to the part
        return self.model is not None and dev.pid == self.model.part.usbpid

    def _tool_labels(self, dev):
        return [k for k, v in self.pgms.items() if self._matches(v, dev)]

    def _is_tool(self, dev):
        """A recognised programmer (not merely a USB-UART bridge)."""
        return bool(self._tool_labels(dev)) and not (dev.vid in UART_BRIDGE_VIDS and dev.ports)

    def _relevant(self, devs):
        """USB devices worth mentioning: programmers and serial adapters."""
        return [d for d in devs if d.ports or self._tool_labels(d)]

    def _port_owner(self, port):
        return next((d for d in self.usb_devs if port in d.ports), None)

    def _detected(self):
        """label -> rank for programmers whose hardware is connected, narrowed
        by the port: usb:<sn> = that device, serial port = serial programmers
        and USB programmers of the device behind that port."""
        pm = self.model.part.prog_modes
        port = self.current_port()
        usb_sn = port[4:] if port.startswith("usb:") else None
        tty = port if port and not port.startswith("usb") else None
        # A serial port counts as a hint for serial programmers only if it is
        # not just the CDC port of a recognised programmer (Snap, nEDBG, ...)
        have_tty = bool(tty) or any(d.ports and not self._is_tool(d) for d in self.usb_devs)
        sdev = self._port_owner(tty) if tty else \
            next((d for d in self.usb_devs if d.ports and not self._is_tool(d)), None)
        spref = serial_pref_for(sdev)
        out = {}
        for label, info in self.pgms.items():
            if not info[1] & pm:
                continue
            if info[3] == ad.CONNTYPE_USB:
                for d in self.usb_devs:
                    if usb_sn is not None and d.serial != usb_sn:
                        continue
                    if tty and tty not in d.ports:
                        continue
                    if self._matches(info, d):
                        bridge = d.vid in UART_BRIDGE_VIDS and d.ports
                        out[label] = (2 if bridge else 0, pm_rank(info[1] & pm), info[0])
                        break
            elif info[3] == ad.CONNTYPE_SERIAL and have_tty and usb_sn is None:
                r = spref.index(info[0]) if info[0] in spref else len(spref)
                out[label] = (1, r, info[0])
        return out

    def _fill_programmers(self):
        if self.model is None:
            return
        mode = self.filter_cb.get()
        self.settings["pgm_filter"] = mode
        pm = self.model.part.prog_modes
        cur = self.pgm_cb.get()
        self._det = det = self._detected()
        if mode == "detected" and det:
            vals = sorted(det, key=det.get)
        else:
            vals = sorted((k for k, v in self.pgms.items() if mode == "all" or v[1] & pm),
                          key=str.lower)
        if cur and cur not in vals:
            vals.append(cur)
        self.pgm_cb.set_values(vals)
        if not cur:
            want = self.settings.get("programmer")
            lab = next((k for k, v in self.pgms.items() if v[0] == want), None)
            if lab:
                self.pgm_cb.set_committed(lab)
        self._update_hw_label()

    def _filter_changed(self):
        self._fill_programmers()
        self._auto_select("Programmer filter")

    def _auto_select(self, reason, only=None):
        """In 'detected' mode pick the best detected programmer if the current
        one is not detected (or, with ``only``, the best one for that device)."""
        if self.filter_cb.get() != "detected" or not self._det:
            return
        cands = self._det
        if only is not None:
            cands = {k: r for k, r in cands.items()
                     if self._matches(self.pgms[k], only) or
                     (self.pgms[k][3] == ad.CONNTYPE_SERIAL and only.ports
                      and not self._is_tool(only))}
            if not cands:
                return
        elif self.pgm_cb.get() in cands:
            return
        best = min(cands, key=cands.get)
        if best != self.pgm_cb.get():
            self.pgm_cb.set_committed(best)
            self.programmer_selected(best)
            self.log(f"{reason}: selected programmer {self.pgms[best][0]}", tag="notice")
            self._fill_programmers()           # drop the previous, undetected entry

    def _fill_ports(self):
        tools = [d for d in self.usb_devs if self._is_tool(d)]
        names = list(dict.fromkeys(d.name() for d in tools))
        vals = ["usb" + (PORT_SEP + ", ".join(names) if names else "")]
        if len(tools) > 1:
            vals += [f"usb:{d.serial}{PORT_SEP}{d.describe(serial=False)}"
                     for d in tools if d.serial]
        # Serial ports of recognised USB debuggers/programmers (Snap, nEDBG,
        # Atmel-ICE, ...) are only a UART bridge to the target, not a way to
        # program it: leave them out unless that port is the current one.
        cur = self.current_port()
        owners = {p: d for d in self.usb_devs for p in d.ports}
        vals += [p + PORT_SEP + d.describe() for p, d in sorted(owners.items())
                 if not self._is_tool(d) or p == cur]
        vals += [p for p in list_serial_ports() if p not in owners]
        self.port_cb["values"] = vals

    def _port_changed(self, _e=None):
        p = self.current_port()
        if self.port_cb.get() != p:
            self.port_cb.set(p)
        if p != getattr(self, "_last_port", None):
            self._last_port = p
            self._fill_programmers()

    def _update_hw_label(self):
        info = self.pgms.get(self.pgm_cb.get())
        port = self.current_port()
        short = full = ""
        ok = None
        if info and info[3] == ad.CONNTYPE_USB and info[4]:
            devs = [d for d in self.usb_devs if self._matches(info, d)
                    and (not port.startswith("usb:") or d.serial == port[4:])]
            if devs:
                ok, short, full = True, devs[0].name(), devs[0].describe()
                if len(devs) > 1:
                    short += f" (+{len(devs) - 1})"
                    full += (f"\n+ {len(devs) - 1} more of the same kind: use "
                             "usb:<serial number> as port to pick one")
            else:
                ok, short = False, "not connected"
                full = f"No USB device {info[4]:04X}:" + \
                    "/".join(f"{p:04X}" for p in info[5]) + " found"
        elif info and info[3] == ad.CONNTYPE_SERIAL:
            d = self._port_owner(port)
            if d:
                ok, short, full = True, d.name(), f"{d.describe()} on {port}"
            elif port.startswith("usb") or not port:
                ok, short = False, "select a serial port"
            elif os.name != "nt" and not os.path.exists(port):
                ok, short = False, "port not found"
        mark = "\u2713 " if ok else "\u2717 " if ok is False else ""
        self.hw_lbl.configure(text=mark + short, foreground="#109030" if ok else
                              "#d02020" if ok is False else "")
        self.hw_tip.text = full or short

    def programmer_selected(self, text, probe=False):
        if probe:   # allow typing an id directly
            t = text.lower()
            return next((k for k, v in self.pgms.items() if t in [i.lower() for i in v[2]]), None)
        info = self.pgms[text]
        self.settings["programmer"] = info[0]
        port = self.current_port()
        if info[3] == ad.CONNTYPE_USB and not port.startswith("usb"):
            devs = [d for d in self.usb_devs if self._matches(info, d)]
            self.port_cb.set("usb" if len(devs) <= 1 or not devs[0].serial
                             else f"usb:{devs[0].serial}")
        elif info[3] == ad.CONNTYPE_SERIAL and (port.startswith("usb") or not port):
            # prefer a port of a plain USB serial adapter / board
            owned = {p for d in self.usb_devs for p in d.ports}

            def fit(pd):        # how well does this programmer suit the device?
                pref = serial_pref_for(pd[1])
                return (pref.index(info[0]) if info[0] in pref else len(pref), pd[0])
            cands = sorted(((p, d) for d in self.usb_devs if not self._is_tool(d)
                            for p in d.ports), key=fit)
            ports = [p for p, _ in cands] or [p for p in list_serial_ports() if p not in owned]
            if ports:
                self.port_cb.set(ports[0])
        self._last_port = self.current_port()
        self._update_hw_label()
        return text

    # -- USB hot-plug -----------------------------------------------------------
    def _scan_worker(self):
        self._scan_result = scan_usb()

    def _scan_tick(self):
        try:
            t = self._scan_thread
            if t is None or not t.is_alive():
                if self._scan_result is not None and not self.busy:
                    res, self._scan_result = self._scan_result, None
                    self._apply_scan(res)
                if self._scan_result is None and \
                        time.monotonic() - self._scan_started >= SCAN_INTERVAL:
                    self._scan_started = time.monotonic()
                    self._scan_thread = threading.Thread(target=self._scan_worker,
                                                         daemon=True)
                    self._scan_thread.start()
        finally:
            self.root.after(300, self._scan_tick)

    def _apply_scan(self, devs):
        old = {d.key(): d for d in self.usb_devs}
        new = {d.key(): d for d in devs}
        if old.keys() == new.keys():
            return
        self.usb_devs = devs
        added = self._relevant([new[k] for k in new if k not in old])
        removed = self._relevant([old[k] for k in old if k not in new])
        for d in removed:
            self.log(f"Disconnected {d.describe()}", tag="notice")
        for d in added:
            self.log(f"Connected {d.describe()}"
                     + (f" on {', '.join(d.ports)}" if d.ports else ""), tag="notice")
        port = self.current_port()
        gone = [d for d in old.values() if d.key() not in new]
        if port.startswith("usb:") and not any(d.serial == port[4:] for d in devs):
            self.port_cb.set("usb")            # that particular programmer is gone
        elif port and not port.startswith("usb") and any(port in d.ports for d in gone):
            self.port_cb.set("usb")            # its serial port is gone
        self._last_port = self.current_port()
        pm = self.model.part.prog_modes
        usable = [d for d in added if any(
            v[1] & pm and (self._matches(v, d) if self._is_tool(d)
                           else v[3] == ad.CONNTYPE_SERIAL and d.ports)
            for v in self.pgms.values())]
        if usable:
            d = usable[-1]                     # plugging in = intent to use it
            if self._is_tool(d):
                tools = [x for x in devs if self._is_tool(x)]
                self.port_cb.set(f"usb:{d.serial}" if len(tools) > 1 and d.serial else "usb")
            else:
                self.port_cb.set(d.ports[0])
            self._last_port = self.current_port()
            self._fill_programmers()
            self._auto_select(f"Connected {d.name()}", only=d)
        else:
            self._fill_programmers()
            if removed:   # fall back to other connected hardware, if any
                self._auto_select(f"Disconnected {removed[-1].name()}")

    def device_selected(self, text, probe=False):
        if probe:
            return self.part_ids.get(text.lower())
        p = self.parts[text]
        self.model = PartModel(p)
        self.settings["device"] = text
        self.fuses.set_model(self.model)
        self.locks.set_model(self.model)
        self.flash.set_model(self.model)
        self.eeprom.set_model(self.model)
        self.sig_lbl.configure(text="Sig " + p.signature.hex(" ").upper(), foreground="")
        self._fill_programmers()
        if hasattr(self, "_scan_thread"):      # not during start-up
            self._auto_select(f"{text}")
        self._update_minsize()
        return text

    def _update_minsize(self):
        # never allow the window to become smaller than what keeps all
        # buttons and value fields visible
        self.root.update_idletasks()
        self._nb_min = self.nbf.winfo_reqheight()
        self.pw.paneconfigure(self.nbf, minsize=self._nb_min)
        self.root.update_idletasks()
        # everything except the paned window, plus the paned window at its minimum
        other = self.root.winfo_reqheight() - self.pw.winfo_reqheight()
        self.root.minsize(max(600, self.root.winfo_reqwidth()),
                          other + self._nb_min + self.LOG_MIN + self.SASH)
        self._layout_panes()

    def _layout_panes(self, _e=None):
        # run after the paned window's own re-arrangement, coalesced
        if not getattr(self, "_layout_pending", False):
            self._layout_pending = True
            self.pw.after_idle(self._do_layout_panes)

    def _do_layout_panes(self):
        self._layout_pending = False
        h = self.pw.winfo_height()
        if h <= 1 or not self._nb_min:
            return
        log_h = max(self.LOG_MIN, min(self.log_pref, h - self._nb_min - self.SASH))
        y = max(self._nb_min, h - log_h - self.SASH)
        if self.pw.sash_coord(0)[1] != y:
            self.pw.sash_place(0, 0, y)

    def _sash_moved(self, _e=None):
        h = self.pw.winfo_height()
        y = self.pw.sash_coord(0)[1]
        self.log_pref = max(self.LOG_MIN, h - y - self.SASH)
        self.settings["log_height"] = self.log_pref

    # -- logging -------------------------------------------------------------
    def log(self, text, level=ad.MSG_INFO, tag=None, nl=True):
        if level > ad.cvar.verbose and level > ad.MSG_INFO:
            return
        if tag is None:
            tag = {ad.MSG_EXT_ERROR: "error", ad.MSG_ERROR: "error",
                   ad.MSG_WARNING: "warning"}.get(level)
            if tag is None:
                tag = "notice" if level in (ad.MSG_NOTICE, ad.MSG_NOTICE2) else \
                    "debug" if level >= ad.MSG_DEBUG else None
        t = self.logtxt
        t.configure(state="normal")
        t.insert("end", text + ("\n" if nl and not text.endswith("\n") else ""),
                 (tag,) if tag else ())
        t.see("end")
        t.configure(state="disabled")
        if level <= ad.MSG_ERROR:
            self.status.configure(text=text.strip().splitlines()[0][:90])
        self._pump()

    # -- menu bar (macOS only: there it is global and Tk's default one has
    #    "Source...", "Run Widget Demo" etc.) ----------------------------------
    def _build_menu(self):
        if self.root.tk.call("tk", "windowingsystem") != "aqua":
            return
        r = self.root
        mb = tk.Menu(r)
        app = tk.Menu(mb, name="apple")
        app.add_command(label=f"About {APP_NAME}", command=self.about)
        mb.add_cascade(menu=app)
        fm = tk.Menu(mb)
        fm.add_command(label="Open Flash File\u2026", accelerator="Command-O",
                       command=self._open_flash)
        fm.add_command(label="Save Log\u2026", command=self.save_log)
        fm.add_separator()
        fm.add_command(label="Close", accelerator="Command-W", command=self.quit)
        mb.add_cascade(label="File", menu=fm)
        # macOS adds AutoFill to any menu titled Edit; Dictation and Emoji
        # are switched off in _mac_hide_menu_extras(), AutoFill cannot be.
        em = tk.Menu(mb)
        for lbl, ev, key in (("Cut", "<<Cut>>", "X"), ("Copy", "<<Copy>>", "C"),
                             ("Paste", "<<Paste>>", "V")):
            em.add_command(label=lbl, accelerator=f"Command-{key}",
                           command=lambda ev=ev: self._edit_event(ev))
        em.add_separator()
        em.add_command(label="Select All", accelerator="Command-A",
                       command=lambda: self._edit_event("<<SelectAll>>"))
        mb.add_cascade(label="Edit", menu=em)
        mb.add_cascade(label="Window", menu=tk.Menu(mb, name="window"))
        # Tk adds "<App> Help" itself; it calls tk::mac::ShowHelp
        mb.add_cascade(label="Help", menu=tk.Menu(mb, name="help"))
        r.configure(menu=mb)
        # Tk calls the tk::mac::* hooks directly from AppKit, outside the Tcl
        # event loop - a Python callback there crashes ("PyEval_RestoreThread:
        # GIL is released"). So the hooks are plain Tcl procs that only queue
        # the Python callback for the event loop.
        r.createcommand("guidude_quit", self.quit)              # Cmd-Q saves settings
        r.createcommand("guidude_help", lambda: webbrowser.open(
            "https://avrdudes.github.io/avrdude/"))
        r.tk.eval("proc ::tk::mac::Quit {} {after idle guidude_quit}")
        r.tk.eval("proc ::tk::mac::ShowHelp {} {after idle guidude_help}")

    def _callback_error(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            self.log("Internal error:\n" + text, ad.MSG_ERROR)
            messagebox.showerror(APP_NAME, f"Internal error: {val!r}\n\nDetails are in the log.")
        except Exception:
            pass

    def _edit_event(self, ev):
        w = self.root.focus_get()
        if w is not None:
            w.event_generate(ev)

    def _open_flash(self):
        self.nb.select(self.flash)
        self.flash.browse()

    def about(self):
        conf = find_config_file()
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "A lightweight front end for AVRDUDE, in the spirit of AVRFuses.\n\n"
            f"libavrdude {ad.cvar.version}\nTk {tk.TkVersion}, Python "
            f"{platform.python_version()}\n\nConfiguration: {conf}\n\n"
            "License: GPL-2.0-or-later")

    def save_log(self):
        f = filedialog.asksaveasfilename(title="Save log", defaultextension=".txt",
                                         initialfile="guidude-log.txt",
                                         filetypes=[("Text", "*.txt"), ("All files", "*")])
        if f:
            try:
                Path(f).write_text(self.logtxt.get("1.0", "end-1c"))
                self.log(f"Log saved to {f}", tag="ok")
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Cannot save log:\n{e}")

    def clear_log(self):
        self.logtxt.configure(state="normal")
        self.logtxt.delete("1.0", "end")
        self.logtxt.configure(state="disabled")

    def _verbosity(self, _e=None):
        v = dict(LOGLEVELS)[self.verb_cb.get()]
        ad.cvar.verbose = v
        self.settings["verbose"] = v

    def msg_callback(self, target, lno, fname, func, msgmode, msglvl, msg, backslash_v):
        if ad.cvar.verbose < msglvl:
            return
        t = self.logtxt
        at_bol = t.get("end-2c", "end-1c") in ("\n", "")
        s = ""
        if (backslash_v or msgmode & ad.MSG2_LEFT_MARGIN) and not at_bol:
            s = "\n"
        if msgmode & ad.MSG2_PROGNAME:
            if ad.cvar.verbose >= ad.MSG_NOTICE and msgmode & ad.MSG2_FUNCTION:
                s += func + "(): "
            if msgmode & ad.MSG2_TYPE:
                s += {ad.MSG_EXT_ERROR: "OS error: ", ad.MSG_ERROR: "error: ",
                      ad.MSG_WARNING: "warning: "}.get(msglvl, "")
        elif msgmode & (ad.MSG2_INDENT1 | ad.MSG2_INDENT2):
            s += "  "
        s += msg
        self.log(s, msglvl, nl=False)

    def progress_callback(self, percent, etime, hdr, finish):
        if hdr:
            self._prog_hdr = hdr
        if percent < 0:
            percent = 0
        self.progress["value"] = percent
        self.status.configure(text=f"{self._prog_hdr} {percent:3d} %  ({etime:.1f} s)")
        self._pump()

    def _pump(self):
        # Keep the window alive during long blocking libavrdude calls.
        # All action buttons are disabled while busy, so re-entrancy is harmless.
        now = time.monotonic()
        if now - self._last_update > 0.05:
            self._last_update = now
            try:
                self.root.update()
            except tk.TclError:
                pass

    # -- programmer sessions -------------------------------------------------------
    def _set_busy(self, busy):
        self.busy = busy
        st = "disabled" if busy else "normal"
        for b in self.action_buttons:
            b.configure(state=st)
        for c in (self.dev_cb, self.pgm_cb, self.port_cb):
            c.configure(state=st)
        self.root.configure(cursor="watch" if busy else "")
        if not busy:
            # editors may have disabled buttons for parts without fuses
            for ed in (self.fuses, self.locks):
                if not ed.mems:
                    for b in (ed.btn_read, ed.btn_verify, ed.btn_prog, ed.btn_def):
                        b.configure(state="disabled")

    def session(self, job, what, check_sig=True, part=None, quiet=False):
        """Open the programmer, run job(pgm, part), close. Returns job's result
        or None on error (already reported). ``part`` overrides the selected
        device (used for probing); ``quiet`` reports failures only in the log.
        After a failure, self.fail_stage is "open", "init" or "job"."""
        self.fail_stage = None
        if self.busy:
            return None
        lab = self.pgm_cb.get()
        if lab not in self.pgms:
            messagebox.showwarning(APP_NAME, "Please select a programmer first.")
            return None
        pid = self.pgms[lab][0]
        port = self.current_port() or "usb"
        p = part or self.model.part
        pgm = ad.locate_programmer(ad.cvar.programmers, pid)
        if pgm is None:
            self.log(f"Programmer {pid} not found", ad.MSG_ERROR)
            return None
        if not (pgm.prog_modes & p.prog_modes) and part is None:
            if not messagebox.askyesno(APP_NAME, f"{pid} does not seem to support any "
                                       f"programming interface of {p.desc}. Try anyway?"):
                return None
        self.settings["port"] = port
        self.settings["bitclock"] = self.bitclock.get().strip()
        self._set_busy(True)
        self.progress["value"] = 0
        self.status.configure(text=f"{what}…")
        self.log(f"── {what}: {p.desc} via {pid} on {port}", tag="notice")
        opened = False
        result = None
        self.fail_stage = "open"
        try:
            ad.init_cx(pgm)
            pgm.initpgm()
            pgm.setup()
            if pgm.open(port) < 0:
                if pgm.conntype == ad.CONNTYPE_SERIAL and os.path.exists(port):
                    raise RuntimeError(
                        f"no valid answer from {pid} on {port} - the port exists, so "
                        f"probably the programmer type does not match the hardware "
                        f"(e.g. stk500v2 for STK500v2-compatible ISP programmers, "
                        f"arduino for Arduino boards)")
                raise RuntimeError(f"cannot open {pid} on port {port}")
            opened = True
            bc = self.bitclock.get().strip()
            if bc:
                try:
                    pgm.set_sck_period(float(bc) * 1e-6)
                except (ValueError, RuntimeError) as e:
                    self.log(f"Bit clock not set: {e}", ad.MSG_WARNING)
            self.fail_stage = "init"
            ad.avr_initmem(p)
            pgm.enable(p)
            rc = pgm.initialize(p)
            if rc < 0:
                raise RuntimeError(f"initialization failed (rc={rc}) - check wiring, "
                                   "target power and clock (try a larger bit clock), "
                                   "or another device is connected: try Detect")
            self.fail_stage = "job"
            if check_sig:
                sig = self._read_sig(pgm, p)
                if sig is not None and sig != p.signature:
                    self._sig_mismatch(sig, pgm)
                    return None
            result = job(pgm, p)
            self.fail_stage = None
            self.status.configure(text=f"{what}: done")
            return result
        except RuntimeError as e:
            if quiet:
                self.log(f"{what} with {p.desc} settings: {str(e).split(' - ')[0]}",
                         tag="notice")
            else:
                self.log(f"{what} failed: {e}", ad.MSG_ERROR)
                self.status.configure(text=f"{what} failed")
                messagebox.showerror(APP_NAME, f"{what} failed:\n\n{e}")
            return None
        finally:
            try:
                if opened:
                    pgm.disable()
                    pgm.close()
                pgm.teardown()
            except Exception as e:     # pragma: no cover
                self.log(f"Error while closing programmer: {e}", ad.MSG_WARNING)
            self.progress["value"] = 0
            self._set_busy(False)

    def _read_sig(self, pgm, p):
        m = ad.avr_locate_mem(p, "signature")
        if m is None:
            return None
        q = ad.cvar.quell_progress
        ad.cvar.quell_progress = 2
        try:
            if ad.avr_read_mem(pgm, p, m) < 0:
                raise RuntimeError("cannot read signature")
        finally:
            ad.cvar.quell_progress = q
        sig = m.get(3)
        ok = sig == self.model.part.signature
        self.sig_lbl.configure(text="Sig " + sig.hex(" ").upper() + (" ✓" if ok else " ✗"),
                               foreground="#109030" if ok else "#d02020")
        return sig

    def _sig_mismatch(self, sig, pgm):
        if sig in (b"\x00\x00\x00", b"\xff\xff\xff"):
            msg = (f"The device answered with signature {sig.hex(' ').upper()}.\n\n"
                   "This usually means no (or no working) connection to the target: "
                   "check wiring, power and clock, or try a larger bit clock.")
            self.log("Invalid signature " + sig.hex(" ").upper(), ad.MSG_ERROR)
            messagebox.showerror(APP_NAME, msg)
            return
        cand = ad.locate_part_by_signature_pm(ad.cvar.part_list, sig, pgm.prog_modes) or \
            ad.locate_part_by_signature(ad.cvar.part_list, sig)
        self.log(f"Signature {sig.hex(' ').upper()} does not match {self.model.desc}",
                 ad.MSG_ERROR)
        if cand is not None and cand.desc in self.parts:
            if messagebox.askyesno(APP_NAME, f"Connected device: {cand.desc}\n"
                                   f"Selected device: {self.model.desc}\n\n"
                                   f"Switch to {cand.desc}? (Then repeat the action.)"):
                self.root.after(10, lambda: self._switch_device(cand.desc))
        else:
            messagebox.showerror(APP_NAME, f"Unknown signature {sig.hex(' ').upper()}.")

    def _switch_device(self, desc):
        self.dev_cb.set_committed(desc)
        self.device_selected(desc)
        self.sig_lbl.configure(text="Sig " + self.parts[desc].signature.hex(" ").upper()
                               + " ✓", foreground="#109030")

    def _updi_probe_parts(self):
        """One representative part per UPDI family and address width. UPDI
        programmers send a family-specific device descriptor at sign-on, so
        e.g. a tinyAVR does not answer while an AVR DA is selected."""
        if getattr(self, "_updi_probes", None) is None:
            groups = {}
            for p in self.parts.values():
                if not p.prog_modes & ad.PM_UPDI:
                    continue
                m = re.match(r"AVR\d+([A-Z]{2})", p.desc)
                fam = m[1] if m else p.family_id.strip()
                fl = ad.avr_locate_mem(p, "flash")
                groups.setdefault((fam, bool(fl and fl.size > 0x10000)), p)
            self._updi_probes = groups
        cur = self.model.part
        m = re.match(r"AVR\d+([A-Z]{2})", cur.desc)
        fl = ad.avr_locate_mem(cur, "flash")
        cur_key = (m[1] if m else cur.family_id.strip(), bool(fl and fl.size > 0x10000))
        return [p for k, p in self._updi_probes.items() if k != cur_key]

    def detect(self):
        def job(pgm, p):
            return self._read_sig(pgm, p)
        info = self.pgms.get(self.pgm_cb.get())
        probes = [self.model.part]
        if info and info[1] & ad.PM_UPDI and self.model.part.prog_modes & ad.PM_UPDI:
            probes += self._updi_probe_parts()
        sig = None
        for i, p in enumerate(probes):
            if i == 1:
                self.log("No answer with these device settings - trying the other "
                         "UPDI device families", tag="notice")
            sig = self.session(job, "Detecting device", check_sig=False, part=p,
                               quiet=i < len(probes) - 1)
            if sig is not None or self.fail_stage != "init":
                break
        if sig is None:
            return
        if sig == self.model.part.signature:
            self.log(f"Detected {self.model.desc} (signature {sig.hex(' ').upper()})", tag="ok")
            return
        lab = self.pgm_cb.get()
        pgm = ad.locate_programmer(ad.cvar.programmers, self.pgms[lab][0])
        self._sig_mismatch(sig, pgm)

    def erase_chip(self):
        if not messagebox.askyesno(APP_NAME, f"Erase the entire {self.model.desc}?\n\n"
                                   "Flash, lock bits and (unless EESAVE is set) EEPROM "
                                   "will be erased.", icon="warning"):
            return
        def job(pgm, p):
            if pgm.chip_erase(p) < 0:
                raise RuntimeError("chip erase failed")
            return True
        if self.session(job, "Erasing chip"):
            self.log("Chip erased", tag="ok")

    # -- shutdown --------------------------------------------------------------
    def quit(self):
        if self.busy:
            return
        self.settings["geometry"] = self.root.geometry()
        try:
            self.settings["tab"] = self.nb.index(self.nb.select())
        except tk.TclError:
            pass
        self.settings["port"] = self.current_port()
        self.settings["flash_erase"] = self.flash.erase.get()
        self.settings["flash_verify"] = self.flash.verify_after.get()
        self.settings["eeprom_verify"] = self.eeprom.verify_after.get()
        try:
            self.sp.parent.mkdir(parents=True, exist_ok=True)
            self.sp.write_text(json.dumps(self.settings, indent=2))
        except OSError:
            pass
        ad.set_msg_callback(None)
        ad.set_progress_callback(None)
        self.root.destroy()


def selftest():
    """Non-GUI smoke test (useful for checking a PyInstaller build)."""
    ad.init_cx()
    ad.init_config()
    conf = find_config_file()
    print(f"{APP_NAME} {APP_VERSION}, libavrdude {ad.cvar.version}")
    print(f"avrdude.conf: {conf}")
    if conf is None or ad.read_config(str(conf)) != 0:
        return 1
    print(f"{ad.lsize(ad.cvar.part_list)} parts, {ad.lsize(ad.cvar.programmers)} programmers")
    p = ad.locate_part(ad.cvar.part_list, "m328p")
    pgm = ad.locate_programmer(ad.cvar.programmers, "dryrun")
    ad.init_cx(pgm)
    pgm.initpgm()
    pgm.setup()
    pgm.open("usb")
    ad.avr_initmem(p)
    pgm.enable(p)
    pgm.initialize(p)
    m = ad.avr_locate_mem(p, "signature")
    ad.avr_read_mem(pgm, p, m)
    sig = m.get(3)
    pgm.disable()
    pgm.close()
    pgm.teardown()
    print(f"dryrun signature of {p.desc}: {sig.hex(' ').upper()}")
    import tkinter
    print(f"Tk {tkinter.TkVersion}")
    return 0 if sig == p.signature else 1


def _mac_hide_menu_extras():
    """macOS adds AutoFill, Start Dictation and Emoji & Symbols to any "Edit"
    menu. Switch them off for this process only (registration domain, nothing
    is written to the user's defaults)."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        def send(obj, sel, *args, argtypes=()):
            f = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, *argtypes))
            return f(obj, objc.sel_registerName(sel.encode()), *args)

        def nsstr(t):
            return send(objc.objc_getClass(b"NSString"), "stringWithUTF8String:",
                        t.encode(), argtypes=(ctypes.c_char_p,))

        def nsbool(b):
            return send(objc.objc_getClass(b"NSNumber"), "numberWithBool:", b,
                        argtypes=(ctypes.c_bool,))
        d = send(objc.objc_getClass(b"NSMutableDictionary"), "dictionary")
        for key, val in (("NSDisabledDictationMenuItem", True),
                         ("NSDisabledCharacterPaletteMenuItem", True),
                         ("NSDisabledAutoFillMenuItem", True),
                         ("NSAutoFillHeuristicControllerEnabled", False)):
            send(d, "setObject:forKey:", nsbool(val), nsstr(key),
                 argtypes=(ctypes.c_void_p, ctypes.c_void_p))
        ud = send(objc.objc_getClass(b"NSUserDefaults"), "standardUserDefaults")
        send(ud, "registerDefaults:", d, argtypes=(ctypes.c_void_p,))
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    _mac_hide_menu_extras()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
