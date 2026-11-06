# Decky Dictation PTT

**Offline voice dictation for Steam Deck Game Mode — with push-to-talk on the back paddles.**

By **LinuxGmr and Asuka**.

Speak, and the text is typed into whatever app has focus: game chat, store
search, browser fields, anything. Recognition runs 100% on-device with the
Vosk speech recognizer — no account, no cloud, no audio ever leaves your Deck.

*Not to be confused with the older, unmaintained "Decky Dictation" plugin:
this is a separate plugin, written independently. It works in any focused
field (including the home UI, store search, and Steam chat, where the older
one does not), adds push-to-talk on the back paddles, and needs no
nerd-dictation dependency — the backend is Python standard library only.*

## How to use it

**Push-to-talk (the best way):** click into the text box you want to type in,
then **hold both back paddles — L4 and R4 — together, wait at least two seconds,
and then start speaking.** Waiting that beat before you talk is what makes it
capture everything. Keep holding while you speak; let go of the paddles to
stop, and your words are typed out.

**Toggle mode:** open the Quick Access Menu → Decky → Decky Dictation PTT and flip
the Dictation switch on, speak, then flip it off when you're done.

### A note about the back paddles

Push-to-talk uses **both** back paddles as its button, so think about what
your games bind to L4 and R4:

- If a game has actions bound to the paddles, holding both to dictate can
  trigger those actions too. Consider leaving the paddles unbound in games
  where you chat a lot, or binding just one of them — dictation only listens
  for the two held *together*.
- If your paddles are spoken for, just use **toggle mode** instead: the QAM
  switch starts and stops dictation and leaves both paddles completely free
  for your game.
- The regular on-screen keyboard is unaffected either way — Steam + X still
  brings it up whenever you'd rather type by hand.

## First run

The first time you start dictation, the plugin downloads the Vosk runtime
library (~7 MB, from PyPI) and the small US-English speech model (~40 MB, from
alphacephei.com) into its own data folder and verifies their SHA-256
checksums. After that one download, dictation works completely offline.

## How it works

- The Decky backend (Python, running as root) watches the controller's
  hidraw reports for the L4+R4 chord (byte 13, bits 1+2) with press/release
  debounce, and starts/stops the recognizer accordingly.
- A small recognizer process runs as your session user, captures the
  microphone (`parec`, `pw-record`, or `ffmpeg`, whichever your system has),
  feeds it to libvosk via ctypes — no third-party Python packages — and types
  finished sentences with the bundled `ydotoold`/`ydotool` over `/dev/uinput`.
- Audio is processed in memory and never recorded or transmitted anywhere.

## Plugin backend checklist (for store review)

- Custom backend other than Python: **No** — the backend and recognizer are
  Python 3 (standard library only).
- Third-party FOSS tool without statically linked dependencies: **Yes** —
  `ydotool`/`ydotoold` (AGPL-3.0, source linked in `bin/README.md`) are
  bundled unmodified and run as external daemons; the Vosk shared library
  (Apache-2.0) is downloaded at first run from its official PyPI wheel with a
  pinned checksum rather than bundled, so its provenance is verifiable.
- Custom binary with all dependencies statically linked: **No**.

Speech recognition is the Vosk toolkit, a traditional Kaldi-based offline
recognizer — not an LLM and not a cloud service.

## Testing status

Developed and verified end-to-end on a Steam Deck running CachyOS Deckify
(Game Mode, Decky Loader): toggle mode and the L4+R4 push-to-talk were both
confirmed typing live, with 13+ clean hold/release cycles logged. Stock
SteamOS testing by the review team / community testers is welcome — audio
capture auto-detects `pw-record`, which ships with SteamOS's PipeWire stack.

## Future improvements

- Larger / better language models and dictionary tuning to smooth out the
  small-model hiccups.
- More languages (Vosk publishes models for 20+ languages).
- Optional single-paddle or remappable push-to-talk bindings.

## Credits

Created by **LinuxGmr and Asuka**.

Third-party components: Vosk (Apache-2.0, Alpha Cephei), ydotool (AGPL-3.0,
ReimuNotMoe), the Decky plugin template (BSD-3-Clause, Steam Deck Homebrew).
See `LICENSE` and `bin/README.md`.

## License

BSD-3-Clause — see `LICENSE` (which also includes the Decky template's
original license, as required for store distribution).
