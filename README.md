# GUIDude: A lightweight GUI for avrdude

GUIDude is a lightweight graphical user interface for avrdude written in Python and making use of the libavrdude library. You can read and set all fuses and lock bits of an AVR MCU. In addition, you can program flash and EEPROM and dump the contents of these memories. 

## Installation of pre-built binaries

Installing pre-built binaries is as simple as downloading the program from the release page of the GitHub repo, unzipping it, and copying the executable `GUIDude`[`.exe`] and the folder `_internals` to a place in your `PATH`. 

On **macOS**, things are a bit more involved, however:

1. Download `GUIDude-macOS-`[`arm64`|`x86_64`]`.zip` from Releases, unzip it, and move `GUIDude.app` to the `Applications` folder.
2. GUIDude is not notarized by Apple, so the first start is blocked. Open *System Settings → Privacy & Security*, scroll down and click **Open Anyway** next to the message about GUIDude, then confirm. Alternatively, run `xattr -dr com.apple.quarantine /Applications/GUIDude.app`
   in Terminal.

Under **Linux**, it may be necessary to install some `udev` rules and assign the user to a group that is allowed to access the serial lines:

1. Copy the udev rules, which you find alongside the executable, to the right place: 
   - `sudo cp 99-guidude.rules /etc/udev/rules.d/`
   - `sudo udevadm control --reload-rules`
   - Unplug and replug the programmer.
2. Assign the user to the right group, usually `dialout`: `sudo usermod -aG dialout $USER`

## Building from source

### macOS

1. You need to install all the necessary packages:
   - Install all necessary packages `brew install swig cmake libusb hidapi libftdi libelf`
   - Install a Python package with Tk support: `brew python-tk@3.14`
2. Build `libavrdude`: 
   - Download the `avrdude` and the `guidude` repo.
   - Change into the root of the `guidude` repo.
   - `./build_lib.sh # will build with SWIG and copy the module and avrdude.conf to lib/`. This works if the `avrdude` repo is stored alongside `guidude`. Otherwise, you can provide a parameter to the build script specifying the location of the `avrdude` repo.
3. Start program: `python3 guided.py`

