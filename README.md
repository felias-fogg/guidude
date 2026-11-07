# GUIDude: A lightweight GUI for avrdude

GUIDude is a lightweight graphical user interface for avrdude written in Python and making use of the libavrdude library. You can read and set all fuses and lock bits of an AVR MCU. In addition, you can program flash and EEPROM and dump the contents of these memories. 

## Installation of pre-built binaries

Download the package for your system from the [Releases](https://github.com/felias-fogg/guidude/releases) page and unpack it. Each package contains an `INSTALL.txt` with the details for that system.

| System | Package | What to do |
|---|---|---|
| **macOS** (Apple Silicon / Intel) | `GUIDude-…-macOS-arm64.zip` / `…-x86_64.zip` | Move `GUIDude.app` to the `Applications` folder. GUIDude is not notarized by Apple, so the first start is blocked: open *System Settings → Privacy & Security*, scroll down and click **Open Anyway** next to the message about GUIDude, then confirm. Alternatively, run `xattr -dr com.apple.quarantine /Applications/GUIDude.app` in Terminal. |
| **Linux** (x86_64 / aarch64) | `GUIDude-…-linux-x86_64.tar.gz` / `…-aarch64.tar.gz` | Start `GUIDude` in the unpacked folder `GUIDude`. Once, install the udev rules and add yourself to the `dialout` group (see `INSTALL.txt`). |
| **Windows** (x64) | `GUIDude-…-windows-x64.zip` | Start `GUIDude.exe` in the unpacked folder `GUIDude`. The program is not signed, so on the first start click *More info* → *Run anyway*. Some USB programmers (USBasp, USBtinyISP, AVRISP mkII, …) need the WinUSB driver, installed once with [Zadig](https://zadig.akeo.ie) (see `INSTALL.txt`). |

On Linux and Windows, the folder can stay where you unpacked it, or be moved anywhere: the executable just has to stay next to the folder `GUIDude_internal`, which holds Python and the libraries. If you want to start GUIDude from the command line, put a link to the executable into a folder in your `PATH`.

## Building from source

GUIDude itself is a single Python file (`guidude.py`) that needs only the Python standard library with Tk. What has to be built is the libavrdude Python module, from the [avrdude](https://github.com/avrdudes/avrdude) sources. The script `build_lib.sh` does that and copies the three files GUIDude needs into `lib/`:

- `_swig_avrdude.so` (`.pyd` on Windows): libavrdude as a Python extension module, made with SWIG
- `swig_avrdude.py`: the SWIG proxy module
- `avrdude.conf`: the part and programmer database

The module only works with the Python version it was built for. So build it with the Python that you will use to run GUIDude, and give that Python to the script as `PYTHON=…` if it is not the first `python3` in your `PATH`.

### 1. Install the build tools

**macOS** (with [Homebrew](https://brew.sh)): the Xcode command line tools (`xcode-select --install`) plus

```sh
brew install cmake swig pkgconf libusb libusb-compat hidapi libftdi python-tk@3.14
```

`python-tk@3.14` installs Python 3.14 together with Tk.

**Linux** (Debian, Ubuntu, Raspberry Pi OS):

```sh
sudo apt install build-essential cmake swig flex bison git \
     python3-dev python3-venv python3-tk \
     libusb-1.0-0-dev libusb-dev libhidapi-dev libftdi1-dev libelf-dev
```

Other distributions have packages of the same names, more or less.

**Windows** (x64):

- [Git for Windows](https://git-scm.com/download/win): the build scripts run in its *Git Bash*.
- Python 3 from [python.org](https://www.python.org/downloads/windows/), which includes Tk.
- Visual Studio 2022 Build Tools with the workload *Desktop development with C++*.
- CMake, SWIG and win_flex_bison, e.g. with `winget install Kitware.CMake SWIG.SWIG WinFlexBison.win_flex_bison`. Put the folders that contain `swig.exe` and `win_bison.exe` into your `PATH`. The links that winget creates are not enough: SWIG and bison then do not find their library files.

The USB libraries (libusb, hidapi, libftdi, libelf) do not need to be installed on Windows: avrdude's CMake build fetches them and links them statically.

### 2. Get the sources

```sh
git clone https://github.com/avrdudes/avrdude.git
git clone https://github.com/felias-fogg/guidude.git
cd guidude
```

GUIDude is tested with avrdude 8.3. To build exactly that version, run `git -C ../avrdude checkout v8.3`.

### 3. Build the libavrdude module

```sh
./build_lib.sh                    # avrdude next to guidude, in ../avrdude
./build_lib.sh path/to/avrdude    # avrdude somewhere else
```

On Windows, run this in Git Bash.

`build_lib.sh` first applies the patches in `avrdude-patches/` to the avrdude source tree, unless they are already in it. They fix problems that only show up when libavrdude is used for several sessions in one program, and they make the PICkit 5 and PICkit Basic work in Windows (MSVC) builds. The patches have been submitted to the avrdude project. On Windows, the patch in `avrdude-patches/libusb/` is applied to avrdude's Windows USB library (`avrdudes/libusb`), which the build then uses.

Optional settings, as environment variables:

| Variable | Meaning |
|---|---|
| `PYTHON` | Python to build the module for (default: `python3`) |
| `DEPS_PREFIX` | where libusb, hidapi and libftdi are installed (macOS default: the Homebrew prefix) |
| `MACOSX_DEPLOYMENT_TARGET` | oldest macOS version to support |
| `AVRDUDE_CMAKE_ARGS` | further arguments for avrdude's CMake, e.g. `-D PYTHON_LIMITED_API=3.10` for one module that works with Python 3.10 and all later versions (stable ABI, "abi3") |

### 4. Run GUIDude

```sh
python3 guidude.py
```

Use the same Python as for the build. `python3 guidude.py --selftest` checks the module without opening a window.

### 5. Optional: build a self-contained application

```sh
./make_dist.sh
```

This installs PyInstaller into a virtual environment `.venv` and builds `dist/GUIDude.app` (macOS) or the folder `dist/GUIDude` (Linux, Windows), which contains Python and all libraries. At the end it runs the self-test of the new application. The release packages on GitHub are built the same way, by the workflow in `.github/workflows/release.yml`.
