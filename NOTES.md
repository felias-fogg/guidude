# GUIDude

Schlanke Tk-Oberfläche (tkinter/ttk) für AVRDUDE im Stil von AVRFuses –
aber direkt über **libavrdude** (SWIG-Modul `swig_avrdude`), ohne die
avrdude-Kommandozeile aufzurufen.

* **Fuses** und **Lock Bits**: jede Bitgruppe als Klartext-Auswahl (aus der
  avrdude-Konfigurationstabelle), Einzelbits klassischer AVRs als Checkbox
  (☑ = programmiert, also 0) – synchron mit den Hex-Feldern darunter.
  Geänderte Hex-Werte sind orange markiert, riskante Einstellungen
  (SPIEN aus, RSTDISBL, DWEN, UPDI-Pin umkonfiguriert, Lock, externe
  Taktquelle) mit ⚠ und einer Rückfrage vor dem Programmieren.
* **Flash** / **EEPROM**: Datei programmieren (optional mit Chip-Erase und
  Verify), verifizieren, in Datei auslesen (Intel-Hex, S-Record, Binär).
* **Detect**: Signatur lesen und passendes Device wählen. Jede Aktion prüft
  die Signatur und bietet bei Abweichung an, auf das erkannte Device zu wechseln.
* **Erkennung angeschlossener Programmer** über USB-VID/PID (macOS: `ioreg`,
  Linux: `/sys`, Windows: PowerShell/CIM, keine Zusatzpakete). Ein- und
  Ausstecken wird automatisch erkannt (alle 2,5 s). Der Programmer-Filter
  „detected“ zeigt nur Programmer, deren Hardware steckt und die das Device
  unterstützen, sortiert nach Interface (UPDI/ISP vor debugWIRE/HV). Serielle
  Programmer erscheinen, wenn ein USB-Seriell-Adapter bzw. Board steckt.
  Der Port schränkt weiter ein (`usb:<SN>` = genau dieses Gerät, serieller
  Port = serielle Programmer und das Gerät dahinter). Beim Einstecken wird der
  passende Programmer samt Port automatisch gewählt; neben dem Port zeigt
  ✓/✗, ob die Hardware des gewählten Programmers angeschlossen ist.
  „for device“ bzw. „all“ zeigen alle passenden bzw. alle Programmer.
* Device und Programmer lassen sich durch Tippen suchen, auch mit
  avrdude-Kurznamen (`m328p`, `t1614`, `avrisp2`).
* Bit clock (wie `-B`) für langsam getaktete Targets.
* Log mit einstellbarer Ausführlichkeit, Fortschrittsbalken.
* Einstellungen (Device, Programmer, Port, zuletzt benutzte Dateien …) werden
  gespeichert (macOS: `~/Library/Application Support/guidude/settings.json`).

Jede Aktion öffnet eine eigene kurze Programmer-Sitzung
(open → initialize → Signatur → Aktion → close), so wie AVRFuses avrdude
pro Aktion aufruft. Man kann also jederzeit umstecken.

## Download

Fertige Programmpakete (macOS-App, später Linux und Windows) gibt es unter
**Releases**. Sie enthalten libavrdude, `avrdude.conf` und alle benötigten
Bibliotheken; Python muss dafür nicht installiert sein.

Die macOS-App ist nur ad-hoc signiert. Beim ersten Start daher im Finder
Rechtsklick → „Öffnen“ wählen oder einmalig `xattr -cr GUIDude.app` ausführen.

## Voraussetzungen

* Python ≥ 3.9 mit Tk (Homebrew: `brew install python-tk@3.14`)
* Zum Bauen von libavrdude: `cmake`, `swig` ≥ 4, libusb, hidapi, libftdi
  (optional `libelf` für ELF-Dateien), z. B. `brew install swig cmake libusb hidapi libftdi libelf`

## Starten

```sh
./build_lib.sh            # baut ../avrdude mit SWIG und kopiert Modul + avrdude.conf nach lib/
python3 guidude.py
```

GUIDude findet das Modul auch direkt in `../avrdude/build_<os>/src`
oder über die Umgebungsvariable `GUIDUDE_LIBDIR`. `avrdude.conf` wird neben
dem Modul gesucht (oder `GUIDUDE_CONF`), `~/.avrduderc` wird zusätzlich gelesen.

Ohne Hardware ausprobieren: Programmer **dryrun** wählen.

## Verteilen mit PyInstaller

```sh
./make_dist.sh            # -> dist/GUIDude.app (macOS) bzw. dist/GUIDude/ (Linux)
```

Die Spec-Datei `guidude.spec` bindet `swig_avrdude` über `pathex` ein,
sodass PyInstaller die Extension samt der verlinkten Bibliotheken
(libusb, hidapi, libftdi …) einsammelt; `avrdude.conf` wird als Datendatei
mitgegeben. `GUIDude --selftest` prüft ein gebautes Bundle ohne GUI.

Hinweise: Das Bundle ist an die Architektur des Build-Rechners gebunden
(arm64 bzw. x86_64) und nur ad-hoc signiert. Für die Weitergabe an Dritte
unter macOS also signieren/notarisieren oder `xattr -cr GUIDude.app`.
Unter Windows funktioniert die Spec analog, sofern `_swig_avrdude.pyd` mit
MSVC/MinGW gebaut und nach `lib/` kopiert wurde.

## Grenzen

Das SWIG-Interface (`libavrdude.i`) exportiert `baudrate` und `bitclock`
nur lesend und keine `-x`-Extended-Parameter. Deshalb gilt:

* Die Baudrate serieller Programmer kommt aus `avrdude.conf` bzw.
  `~/.avrduderc` (z. B. für alte Arduino-Nano-Bootloader dort einen eigenen
  Programmer mit `baudrate = 57600;` anlegen).
* Der Bit-Takt wird über `set_sck_period()` gesetzt. Das unterstützen nicht
  alle Programmer (dann gibt es eine Warnung im Log).

## Lizenz

GPL-2.0-or-later (siehe `LICENSE`), wie AVRDUDE, auf dessen Bibliothek
GUIDude aufbaut. Die Release-Pakete enthalten zusätzlich libusb und
libftdi (LGPL-2.1) sowie hidapi (BSD-Lizenzoption).
