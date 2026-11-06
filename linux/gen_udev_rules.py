#!/usr/bin/env python3
"""Generate linux/99-guidude.rules from avrdude.conf.

Every USB programmer avrdude knows (usbvid/usbpid in avrdude.conf) plus the
DFU bootloaders of USB AVRs (usbpid of the parts) gets a rule that gives
normal users access. Serial programmers (Arduino, STK500v2 clones, ...) need
no rule; the user must be in the "dialout" group instead.

Usage: python3 linux/gen_udev_rules.py [lib-dir]   (default: ./lib)
"""

import ctypes
import sys
from collections import defaultdict
from pathlib import Path

here = Path(__file__).resolve().parent
libdir = Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / "lib"
sys.path.insert(0, str(libdir))
import swig_avrdude as ad  # noqa: E402

ad.init_cx()
ad.init_config()
ad.set_msg_callback(lambda *a: None)
if ad.read_config(str(libdir / "avrdude.conf")) != 0:
    sys.exit("cannot read avrdude.conf")


def iter_list(lst, conv):
    n = ad.lfirst(lst)
    while n:
        yield conv(n)
        n = ad.lnext(n)


ids = defaultdict(set)          # (vid, pid) -> programmer ids
for g in iter_list(ad.cvar.programmers, ad.ldata_programmer):
    names = list(iter_list(g.id, ad.ldata_string))
    if not names or names[0].startswith(".") or g.conntype != ad.CONNTYPE_USB or not g.usbvid:
        continue
    for pid in iter_list(g.usbpid, lambda n: ctypes.c_int.from_address(int(ad.ldata(n))).value):
        ids[(g.usbvid, pid)].add(names[0])

# Debugger modes avrdude does not use, but which firmware updates need
for vid_pid, what in {(0x03EB, 0x2169): "EDBG on Xplained Pro (mass storage mode)",
                      (0x03EB, 0x2FC0): "nEDBG on Curiosity Nano (DFU mode)"}.items():
    ids[vid_pid].add(what)

dfu = defaultdict(set)          # FLIP/DFU bootloaders of USB AVRs (Atmel VID)
for p in iter_list(ad.cvar.part_list, ad.ldata_avrpart):
    if p.usbpid and not p.id.startswith("."):
        dfu[(0x03EB, p.usbpid)].add(p.desc)


def short(names, n=6):
    names = sorted(names, key=str.lower)
    return ", ".join(names[:n]) + (f", ... (+{len(names) - n})" if len(names) > n else "")


out = [
    "# udev rules for GUIDude: give normal users access to the USB programmers",
    "# and debuggers avrdude supports. Generated from avrdude.conf by",
    "# linux/gen_udev_rules.py - do not edit by hand.",
    "#",
    "# To install, type these commands in a terminal:",
    "#   sudo cp 99-guidude.rules /etc/udev/rules.d/",
    "#   sudo udevadm control --reload-rules",
    "# then unplug and reconnect the programmer.",
    "#",
    "# Serial programmers (Arduino boards, STK500v2 clones, USB-serial adapters)",
    "# need no rule here; instead add yourself to the dialout group once:",
    "#   sudo usermod -aG dialout $USER     (then log out and in again)",
    "#",
    "# If you share your Linux system with other users and don't like world",
    "# write access, replace MODE=\"0666\" by GROUP=\"plugdev\", MODE=\"0660\"",
    "# (or another group you are a member of).",
    "",
    "# --- USB programmers and debuggers ---",
]
for (vid, pid), names in sorted(ids.items()):
    out.append(f"# {short(names)}")
    out.append(f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vid:04x}", '
               f'ATTRS{{idProduct}}=="{pid:04x}", MODE="0666"')
    out.append(f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid:04x}", '
               f'ATTRS{{idProduct}}=="{pid:04x}", MODE="0666"')
out += ["", "# --- DFU (FLIP) bootloaders of USB AVRs ---"]
for (vid, pid), names in sorted(dfu.items()):
    if (vid, pid) in ids:
        continue
    out.append(f"# {short(names)}")
    out.append(f'SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vid:04x}", '
               f'ATTRS{{idProduct}}=="{pid:04x}", MODE="0666"')

target = here / "99-guidude.rules"
target.write_text("\n".join(out) + "\n")
print(f"wrote {target}: {len(ids)} programmer IDs, {len(dfu)} DFU IDs")
