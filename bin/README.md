# Bundled binaries

`ydotool` and `ydotoold` (v1.0.4) are from the ydotool project by ReimuNotMoe:

- Source: https://github.com/ReimuNotMoe/ydotool
- License: GNU AGPL-3.0 (see the upstream repository)

They are unmodified binaries (taken from the official CachyOS package of the
same upstream release) and are invoked as external programs by the plugin;
no ydotool code is linked into the plugin itself.

The Vosk native library (`libvosk.so`, Apache-2.0, Alpha Cephei) and the
speech model are NOT in this repository; the plugin downloads them on first
use from their official sources with pinned SHA-256 checksums (see main.py):
the vosk 0.3.45 wheel from PyPI and `vosk-model-small-en-us-0.15` from
alphacephei.com.
