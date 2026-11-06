"""
Decky Dictation backend (store build).

Self-contained edition of Decky Dictation:

- On first use it downloads the Vosk runtime library and the small en-US
  speech model into the plugin's runtime directory (SHA-256 pinned below).
- A bundled, private ydotoold provides /dev/uinput typing.
- The recognizer (dictation_engine.py, Python standard library only) runs
  as the desktop session user so it can reach the microphone.
- Push-to-talk: a thread in this backend watches the Steam Deck
  controller's hidraw reports for the L4+R4 chord and starts/stops
  dictation while the chord is held. No systemd units, no files outside
  the plugin's own folders, no hardcoded usernames.
"""

import asyncio
import glob
import hashlib
import os
import pwd
import select
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

import decky

logger = decky.logger

PLUGIN_DIR = decky.DECKY_PLUGIN_DIR
RUNTIME_DIR = decky.DECKY_PLUGIN_RUNTIME_DIR
ASSETS_DIR = os.path.join(RUNTIME_DIR, "assets")
ENGINE_PATH = os.path.join(PLUGIN_DIR, "dictation_engine.py")
YDOTOOL_BIN = os.path.join(PLUGIN_DIR, "bin", "ydotool")
YDOTOOLD_BIN = os.path.join(PLUGIN_DIR, "bin", "ydotoold")
DAEMON_SOCKET = os.path.join(RUNTIME_DIR, "ydotoold.sock")
ENGINE_LOG = os.path.join(RUNTIME_DIR, "engine.log")

WHEEL_URL = "https://files.pythonhosted.org/packages/fc/ca/83398cfcd557360a3d7b2d732aee1c5f6999f68618d1645f38d53e14c9ff/vosk-0.3.45-py3-none-manylinux_2_12_x86_64.manylinux2010_x86_64.whl"
WHEEL_SHA256 = "25e025093c4399d7278f543568ed8cc5460ac3a4bf48c23673ace1e25d26619f"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_SHA256 = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
MODEL_DIRNAME = "vosk-model-small-en-us-0.15"

# Push-to-talk chord. Steam Deck controller reports are 64 bytes; byte 13
# is the back-paddle bitfield (bit 1 = one paddle, bit 2 = the other), so
# the L4+R4 chord reads as both bits set. Streaks debounce press/release.
PTT_BYTE = 13
PTT_MASK = 0b110
PTT_ON_STREAK = 3
PTT_OFF_STREAK = 5
CONTROLLER_IDS = "000028DE:00001205"  # Valve Steam Deck controller


def _session_user() -> pwd.struct_passwd:
    """The interactive user is uid 1000 on SteamOS ('deck') and most installs."""
    try:
        return pwd.getpwuid(1000)
    except KeyError:
        return pwd.getpwuid(os.getuid())


def _user_env() -> dict:
    pw = _session_user()
    return {
        "HOME": pw.pw_dir,
        "USER": pw.pw_name,
        "LOGNAME": pw.pw_name,
        "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{pw.pw_uid}/bus",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
    }


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: str, sha256: str) -> None:
    if os.path.exists(dest) and _sha256_of(dest) == sha256:
        return
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "decky-dictation/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    digest = _sha256_of(tmp)
    if digest != sha256:
        os.remove(tmp)
        raise RuntimeError(f"checksum mismatch for {url}: got {digest}")
    os.replace(tmp, dest)


class Plugin:
    def __init__(self):
        self._engine = None  # subprocess.Popen, runs as session user
        self._engine_owner = None  # "toggle" | "ptt"
        self._daemon = None  # ydotoold subprocess.Popen
        self._setup_state = "not_started"  # not_started|downloading|ready|error
        self._setup_detail = ""
        self._setup_lock = asyncio.Lock()
        self._loop = None
        self._ptt_thread = None
        self._stopping = False

    # ------------------------------------------------------------- lifecycle

    async def _main(self):
        self._loop = asyncio.get_event_loop()
        logger.info("Decky Dictation backend started (store build)")
        if os.environ.get("DECKY_DICTATION_DISABLE_PTT") != "1":
            self._ptt_thread = threading.Thread(
                target=self._ptt_thread_main, name="ptt-watcher", daemon=True
            )
            self._ptt_thread.start()

    async def _unload(self):
        self._stopping = True
        await self._stop_engine()
        self._stop_daemon()
        logger.info("Decky Dictation backend unloaded")

    # ----------------------------------------------------------------- setup

    def _setup_blocking(self) -> None:
        os.makedirs(ASSETS_DIR, exist_ok=True)
        libvosk = os.path.join(ASSETS_DIR, "vosk", "libvosk.so")
        if not os.path.exists(libvosk):
            self._setup_detail = "downloading the speech engine"
            wheel = os.path.join(ASSETS_DIR, "vosk.whl")
            _download(WHEEL_URL, wheel, WHEEL_SHA256)
            with zipfile.ZipFile(wheel) as z:
                names = [n for n in z.namelist() if n.endswith("libvosk.so")]
                if not names:
                    raise RuntimeError("libvosk.so not found in the vosk wheel")
                os.makedirs(os.path.dirname(libvosk), exist_ok=True)
                with z.open(names[0]) as src, open(libvosk, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.remove(wheel)
        model_dir = os.path.join(ASSETS_DIR, MODEL_DIRNAME)
        if not os.path.isdir(model_dir):
            self._setup_detail = "downloading the speech model (~40 MB, one time)"
            zip_path = os.path.join(ASSETS_DIR, "model.zip")
            _download(MODEL_URL, zip_path, MODEL_SHA256)
            self._setup_detail = "unpacking the speech model"
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(ASSETS_DIR)
            os.remove(zip_path)
        self._setup_detail = ""

    async def _ensure_setup(self) -> bool:
        if self._setup_state == "ready":
            return True
        async with self._setup_lock:
            if self._setup_state == "ready":
                return True
            self._setup_state = "downloading"
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._setup_blocking
                )
                self._setup_state = "ready"
                logger.info("Decky Dictation setup complete")
                return True
            except Exception as exc:
                self._setup_state = "error"
                self._setup_detail = str(exc)
                logger.exception("Decky Dictation setup failed")
                return False

    # ---------------------------------------------------------------- daemon

    def _start_daemon(self) -> bool:
        if self._daemon and self._daemon.poll() is None:
            return True
        try:
            os.remove(DAEMON_SOCKET)
        except OSError:
            pass
        try:
            self._daemon = subprocess.Popen(
                [YDOTOOLD_BIN, "--socket-path", DAEMON_SOCKET],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            logger.exception("failed to launch ydotoold")
            return False
        for _ in range(50):
            if os.path.exists(DAEMON_SOCKET):
                try:
                    os.chmod(DAEMON_SOCKET, 0o666)
                except OSError:
                    pass
                return True
            if self._daemon.poll() is not None:
                logger.error("ydotoold exited during startup")
                return False
            time.sleep(0.1)
        logger.error("ydotoold socket did not appear")
        return False

    def _stop_daemon(self) -> None:
        if self._daemon and self._daemon.poll() is None:
            self._daemon.terminate()
            try:
                self._daemon.wait(timeout=3)
            except Exception:
                self._daemon.kill()
        self._daemon = None

    # ---------------------------------------------------------------- engine

    def _engine_running(self) -> bool:
        return self._engine is not None and self._engine.poll() is None

    async def _start_engine(self, owner: str) -> bool:
        if self._engine_running():
            return True
        if not await self._ensure_setup():
            return False
        if not self._start_daemon():
            self._setup_state = "error"
            self._setup_detail = "could not start the typing daemon (ydotoold)"
            return False
        pw = _session_user()
        env = _user_env()
        python3 = shutil.which("python3", path=env["PATH"]) or "/usr/bin/python3"
        libvosk = os.path.join(ASSETS_DIR, "vosk", "libvosk.so")
        model_dir = os.path.join(ASSETS_DIR, MODEL_DIRNAME)
        try:
            logf = open(ENGINE_LOG, "ab")
            self._engine = subprocess.Popen(
                [
                    python3,
                    ENGINE_PATH,
                    "--libvosk",
                    libvosk,
                    "--model",
                    model_dir,
                    "--ydotool",
                    YDOTOOL_BIN,
                    "--socket",
                    DAEMON_SOCKET,
                ],
                stdout=logf,
                stderr=logf,
                stdin=subprocess.DEVNULL,
                env=env,
                user=pw.pw_uid,
                start_new_session=True,
            )
        except Exception:
            logger.exception("failed to launch dictation engine")
            return False
        self._engine_owner = owner
        await asyncio.sleep(0.5)
        if not self._engine_running():
            logger.error("dictation engine exited during startup; see engine.log")
            self._engine_owner = None
            return False
        logger.info(f"dictation engine started (owner={owner})")
        return True

    async def _stop_engine(self, owner_only: str = None) -> None:
        if owner_only and self._engine_owner not in (None, owner_only):
            return
        if self._engine and self._engine.poll() is None:
            self._engine.terminate()
            for _ in range(30):
                if self._engine.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if self._engine.poll() is None:
                self._engine.kill()
        self._engine = None
        self._engine_owner = None

    # ------------------------------------------------------- frontend methods

    async def start_dictation(self) -> bool:
        logger.info("Decky Dictation: start requested (toggle)")
        await self._start_engine("toggle")
        return self._engine_running()

    async def stop_dictation(self) -> bool:
        logger.info("Decky Dictation: stop requested (toggle)")
        await self._stop_engine()
        return self._engine_running()

    async def toggle_dictation(self) -> bool:
        if self._engine_running():
            await self._stop_engine()
        else:
            await self._start_engine("toggle")
        return self._engine_running()

    async def dictation_running(self) -> bool:
        return self._engine_running()

    async def dictation_status(self) -> dict:
        return {
            "running": self._engine_running(),
            "setup": self._setup_state,
            "detail": self._setup_detail,
        }

    # -------------------------------------------------------------- push-to-talk

    def _call_async(self, coro) -> None:
        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=90)
        except Exception:
            logger.exception("push-to-talk action failed")

    def _open_controllers(self) -> list:
        fds = []
        for uevent_path in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
            try:
                with open(uevent_path) as f:
                    content = f.read()
            except OSError:
                continue
            if CONTROLLER_IDS not in content:
                continue
            dev = "/dev/" + uevent_path.split("/")[4]
            try:
                fds.append(os.open(dev, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                continue
        return fds

    def _ptt_thread_main(self) -> None:
        logger.info("push-to-talk watcher running (L4+R4 chord)")
        while not self._stopping:
            fds = self._open_controllers()
            if not fds:
                time.sleep(5)
                continue
            held = False
            on_streak = 0
            off_streak = 0
            try:
                while not self._stopping:
                    ready, _, _ = select.select(fds, [], [], 0.5)
                    for fd in ready:
                        try:
                            data = os.read(fd, 64)
                        except BlockingIOError:
                            continue
                        if len(data) < PTT_BYTE + 1:
                            continue
                        value = data[PTT_BYTE]
                        if (value & PTT_MASK) == PTT_MASK:
                            on_streak += 1
                            off_streak = 0
                        else:
                            off_streak += 1
                            on_streak = 0
                        if not held and on_streak >= PTT_ON_STREAK:
                            held = True
                            logger.info("PTT HELD - dictation start")
                            self._call_async(self._start_engine("ptt"))
                        elif held and off_streak >= PTT_OFF_STREAK:
                            held = False
                            logger.info("PTT RELEASED - dictation stop")
                            self._call_async(self._stop_engine(owner_only="ptt"))
            except OSError:
                pass  # device went away; reopen below
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            time.sleep(1)
