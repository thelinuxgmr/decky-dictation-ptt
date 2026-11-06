#!/usr/bin/env python3
"""
Decky Dictation PTT recognition engine (store build).

Runs as the desktop session user. Loads libvosk via ctypes (stdlib only, no
third-party Python packages), captures microphone audio from the first
recorder that works, and types recognized sentences into the focused window
with ydotool.

Recorders tried in order:
  1. parec     raw s16le 16 kHz mono   (PulseAudio / pipewire-pulse)
  2. pw-record WAV stream on stdout    (PipeWire, stock SteamOS)
  3. ffmpeg    -f pulse -> raw s16le   (fallback)

Usage:
  dictation_engine.py --libvosk PATH --model DIR --ydotool PATH \
      --socket PATH [--selftest-seconds N] [--no-type]
"""

import argparse
import ctypes
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import time

SAMPLE_RATE = 16000
CHUNK = 8192  # bytes = 4096 samples = 0.256 s at 16 kHz s16le mono


class Vosk:
    def __init__(self, lib_path: str, model_dir: str):
        lib = ctypes.CDLL(lib_path)
        lib.vosk_set_log_level.argtypes = [ctypes.c_int]
        lib.vosk_model_new.argtypes = [ctypes.c_char_p]
        lib.vosk_model_new.restype = ctypes.c_void_p
        lib.vosk_recognizer_new.argtypes = [ctypes.c_void_p, ctypes.c_float]
        lib.vosk_recognizer_new.restype = ctypes.c_void_p
        lib.vosk_recognizer_accept_waveform.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        lib.vosk_recognizer_accept_waveform.restype = ctypes.c_int
        lib.vosk_recognizer_result.argtypes = [ctypes.c_void_p]
        lib.vosk_recognizer_result.restype = ctypes.c_char_p
        lib.vosk_recognizer_final_result.argtypes = [ctypes.c_void_p]
        lib.vosk_recognizer_final_result.restype = ctypes.c_char_p
        lib.vosk_recognizer_free.argtypes = [ctypes.c_void_p]
        lib.vosk_model_free.argtypes = [ctypes.c_void_p]
        lib.vosk_set_log_level(-1)
        self.lib = lib
        self.model = lib.vosk_model_new(model_dir.encode())
        if not self.model:
            raise RuntimeError(f"vosk_model_new failed for {model_dir}")
        self.rec = lib.vosk_recognizer_new(self.model, float(SAMPLE_RATE))
        if not self.rec:
            raise RuntimeError("vosk_recognizer_new failed")

    def accept(self, chunk: bytes) -> bool:
        return (
            self.lib.vosk_recognizer_accept_waveform(self.rec, chunk, len(chunk))
            == 1
        )

    def _text(self, raw: bytes) -> str:
        try:
            return json.loads(raw.decode("utf-8", "replace")).get("text", "").strip()
        except Exception:
            return ""

    def result_text(self) -> str:
        return self._text(self.lib.vosk_recognizer_result(self.rec))

    def final_text(self) -> str:
        return self._text(self.lib.vosk_recognizer_final_result(self.rec))

    def close(self):
        if self.rec:
            self.lib.vosk_recognizer_free(self.rec)
            self.rec = None
        if self.model:
            self.lib.vosk_model_free(self.model)
            self.model = None


class WavStream:
    """Strips a RIFF/WAV header incrementally; passes PCM through after it.

    If the stream does not start with RIFF/WAVE it is treated as raw PCM.
    """

    def __init__(self):
        self.buf = bytearray()
        self.started = False

    def feed(self, chunk: bytes) -> bytes:
        if self.started:
            return chunk
        self.buf += chunk
        if len(self.buf) < 12:
            return b""
        if self.buf[0:4] != b"RIFF" or self.buf[8:12] != b"WAVE":
            self.started = True
            out = bytes(self.buf)
            self.buf.clear()
            return out
        pos = 12
        while pos + 8 <= len(self.buf):
            cid = bytes(self.buf[pos : pos + 4])
            size = struct.unpack("<I", self.buf[pos + 4 : pos + 8])[0]
            if cid == b"data":
                out = bytes(self.buf[pos + 8 :])
                self.buf.clear()
                self.started = True
                return out
            if len(self.buf) < pos + 8 + size:
                return b""  # wait for the rest of this chunk
            pos += 8 + size + (size & 1)
        return b""


def recorder_candidates():
    cands = []
    if shutil.which("parec"):
        cands.append(
            (
                "parec",
                [
                    "parec",
                    "--format=s16le",
                    f"--rate={SAMPLE_RATE}",
                    "--channels=1",
                    "--raw",
                ],
                False,
            )
        )
    if shutil.which("pw-record"):
        cands.append(
            (
                "pw-record",
                [
                    "pw-record",
                    "--format=s16le",
                    f"--rate={SAMPLE_RATE}",
                    "--channels=1",
                    "-",
                ],
                True,
            )
        )
    if shutil.which("ffmpeg"):
        cands.append(
            (
                "ffmpeg",
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-f",
                    "pulse",
                    "-i",
                    "default",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "1",
                    "-f",
                    "s16le",
                    "-",
                ],
                False,
            )
        )
    return cands


def start_recorder(log):
    """Returns (Popen, WavStream|None, first_chunk) for the first recorder
    that produces audio within 3 seconds."""
    for name, cmd, is_wav in recorder_candidates():
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except Exception as exc:
            log(f"recorder {name}: spawn failed: {exc}")
            continue
        fd = proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], 3.0)
        if ready:
            first = os.read(fd, CHUNK)
            if first:
                log(f"recorder: using {name}")
                return proc, (WavStream() if is_wav else None), first
        if proc.poll() is None:
            # Alive but silent so far; accept it (quiet room).
            log(f"recorder: using {name} (no audio yet)")
            return proc, (WavStream() if is_wav else None), b""
        log(f"recorder {name}: exited immediately (rc={proc.returncode})")
    return None, None, b""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--libvosk", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--ydotool", required=True)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--selftest-seconds", type=float, default=0)
    ap.add_argument("--no-type", action="store_true")
    args = ap.parse_args()

    def log(msg):
        print(f"[engine] {msg}", flush=True)

    vosk = Vosk(args.libvosk, args.model)
    log("model loaded")
    proc, wav, pending = start_recorder(log)
    if proc is None:
        log("no working audio recorder found")
        return 3
    env = dict(os.environ)
    env["YDOTOOL_SOCKET"] = args.socket

    def type_text(text: str):
        if args.no_type or not text:
            return
        try:
            subprocess.run(
                [args.ydotool, "type", "--", text + " "],
                env=env,
                timeout=20,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log(f"ydotool type failed: {exc}")

    deadline = time.time() + args.selftest_seconds if args.selftest_seconds else None
    fd = proc.stdout.fileno()
    try:
        while True:
            if pending:
                chunk, pending = pending, b""
            else:
                if proc.poll() is not None:
                    break
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    if deadline and time.time() > deadline:
                        break
                    continue
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
            if wav is not None:
                chunk = wav.feed(chunk)
                if not chunk:
                    continue
            if vosk.accept(chunk):
                text = vosk.result_text()
                if text:
                    log(f"recognized: {text!r}")
                    type_text(text)
            if deadline and time.time() > deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            text = vosk.final_text()
            if text:
                log(f"recognized (final): {text!r}")
                type_text(text)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        vosk.close()
    log("engine exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
