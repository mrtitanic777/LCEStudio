"""Auto-updater — check GitHub Releases for a newer LCEStudio and self-replace.

Ported from the NexiaIDE update system (Electron/TS) and rebranded for
LCEStudio's stack (Python / PyInstaller onefile). The shape is the same:

    * on launch, quietly ask the release channel for the latest version
    * if it is newer than this build, show the user what's new and ask
      whether to install it
    * on "Install now", download the new build, verify it, swap it in, relaunch

The differences are only in the plumbing:

    * The release channel is the **GitHub Releases API** (no private server) —
      ``https://api.github.com/repos/<REPO>/releases/latest``.
    * LCEStudio ships as one self-contained ``LCEStudio.exe`` rather than an
      NSIS installer, so "install" means *self-replace*: download the new exe
      to temp, then hand off to a tiny script that waits for this process to
      exit, copies the new exe over the running one, and relaunches it.

Everything here is pure logic + stdlib (urllib/json/hashlib/subprocess) — no
tkinter, no third-party deps — so it imports cleanly in headless/CLI contexts.
The GUI (``lce/app/gui.py``) drives it and owns all the windows.
"""
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request

# --------------------------------------------------------------------------- #
#  Where updates come from
# --------------------------------------------------------------------------- #
REPO = "mrtitanic777/LCEStudio"
#: the release asset that IS the app (the onefile build we ship + self-replace)
ASSET_NAME = "LCEStudio.exe"
_API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
_UA = "LCEStudio-Updater"


def current_version():
    """This build's version string (``lce.__version__``, e.g. ``"1.0.0"``)."""
    try:
        from lce import __version__ as v
        return str(v)
    except Exception:
        return "0.0.0"


# --------------------------------------------------------------------------- #
#  Version comparison  (semantic-ish: numeric fields, ignores a leading 'v')
# --------------------------------------------------------------------------- #
def parse_version(s):
    """``"v1.2.3"`` / ``"1.2"`` -> ``(1, 2, 3)`` (missing fields are 0)."""
    if not s:
        return (0,)
    nums = re.findall(r"\d+", str(s))
    return tuple(int(n) for n in nums) if nums else (0,)


def cmp_versions(a, b):
    """-1 / 0 / 1 for ``a`` vs ``b`` (pads the shorter with zeros)."""
    pa, pb = parse_version(a), parse_version(b)
    n = max(len(pa), len(pb))
    pa += (0,) * (n - len(pa))
    pb += (0,) * (n - len(pb))
    return (pa > pb) - (pa < pb)


def is_newer(candidate, current=None):
    """True when ``candidate`` is a strictly higher version than this build."""
    return cmp_versions(candidate, current or current_version()) > 0


# --------------------------------------------------------------------------- #
#  Frozen-build helpers  (self-replace only makes sense for the shipped exe)
# --------------------------------------------------------------------------- #
def is_frozen():
    return bool(getattr(sys, "frozen", False))


def current_exe():
    """Path of the running LCEStudio.exe (only meaningful when frozen)."""
    return os.path.abspath(sys.executable)


def release_page():
    return "https://github.com/%s/releases/latest" % REPO


# --------------------------------------------------------------------------- #
#  Check
# --------------------------------------------------------------------------- #
class Update(dict):
    """A newer release: ``version, notes[list[str]], url, size, sha256, name,
    html_url``.  It's a plain dict so the GUI/JSON handle it trivially."""
    __getattr__ = dict.get


def _http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        if r.status != 200:
            raise IOError("HTTP %s" % r.status)
        return json.loads(r.read().decode("utf-8", "replace"))


def _notes_from_body(body, limit=12):
    """Turn a GitHub release body into a short bullet list. Prefers existing
    ``- ``/``* `` bullets; otherwise splits non-empty lines. Markdown-light."""
    if not body:
        return []
    lines = []
    for raw in str(body).splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        s = re.sub(r"^[-*+]\s+", "", s)                 # strip bullet marker
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)          # **bold**
        s = re.sub(r"`([^`]+)`", r"\1", s)              # `code`
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # [text](url)
        if s:
            lines.append(s)
        if len(lines) >= limit:
            break
    return lines


def check(current=None, timeout=8):
    """Ask GitHub for the latest release. Returns an :class:`Update` if it is
    newer than ``current`` (defaults to this build) **and** carries the
    ``LCEStudio.exe`` asset; otherwise ``None``. Never raises — any failure
    (offline, rate-limited, malformed) is swallowed and returns ``None`` so a
    launch check can't ever block or crash the app."""
    try:
        data = _http_json(_API_LATEST, timeout=timeout)
    except Exception:
        return None
    try:
        if data.get("draft") or data.get("prerelease"):
            return None
        tag = data.get("tag_name") or data.get("name") or ""
        version = re.sub(r"^v", "", tag.strip(), flags=re.I)
        if not version:
            return None
        if not is_newer(version, current):
            return None
        # find the shipped exe among the assets
        asset = None
        for a in (data.get("assets") or []):
            if (a.get("name") or "").lower() == ASSET_NAME.lower():
                asset = a
                break
        if not asset:
            return None
        sha = None
        dig = asset.get("digest") or ""                 # e.g. "sha256:abcd..."
        if isinstance(dig, str) and dig.lower().startswith("sha256:"):
            sha = dig.split(":", 1)[1].strip().lower()
        return Update(
            version=version,
            notes=_notes_from_body(data.get("body")),
            url=asset.get("browser_download_url"),
            size=int(asset.get("size") or 0),
            sha256=sha,
            name=asset.get("name") or ASSET_NAME,
            html_url=data.get("html_url") or release_page(),
        )
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Download  (streamed, progress callback, size + optional SHA-256 verify)
# --------------------------------------------------------------------------- #
def download(url, dest, on_progress=None, expected_size=None,
             expected_sha256=None, timeout=30):
    """Stream ``url`` to ``dest``, verifying size and (if known) SHA-256.

    ``on_progress(received, total, pct)`` is called as bytes arrive. Refuses
    non-HTTPS URLs (a hijacked download URL would otherwise be code execution).
    Returns ``(ok: bool, error: str|None, sha256: str)``. The partial file is
    removed on any failure."""
    if not re.match(r"^https://", url or "", re.I):
        return (False, "Refusing to download from a non-HTTPS URL", "")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    ctx = ssl.create_default_context()
    h = hashlib.sha256()
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            if r.status != 200:
                return (False, "Download failed (HTTP %s)" % r.status, "")
            total = int(r.headers.get("Content-Length") or expected_size or 0)
            received = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    received += len(chunk)
                    if on_progress:
                        pct = int(received * 100 / total) if total else 0
                        try:
                            on_progress(received, total, pct)
                        except Exception:
                            pass
        digest = h.hexdigest()
        if expected_size and received != expected_size:
            os.remove(tmp)
            return (False, "Size mismatch — download rejected.", digest)
        if expected_sha256 and digest.lower() != str(expected_sha256).lower():
            os.remove(tmp)
            return (False, "Checksum mismatch — download rejected.", digest)
        os.replace(tmp, dest)
        return (True, None, digest)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return (False, str(e), "")


def temp_download_path(version):
    import tempfile
    return os.path.join(tempfile.gettempdir(), "LCEStudio-%s.exe" % (version or "latest"))


# --------------------------------------------------------------------------- #
#  Install  (self-replace: hand off to a helper that swaps the exe + relaunch)
# --------------------------------------------------------------------------- #
def apply_update(new_exe, target=None, relaunch=True):
    """Swap ``new_exe`` in for the running ``LCEStudio.exe`` and (optionally)
    relaunch it. Windows can't overwrite a running exe, so this spawns a small
    detached batch script that:

        1. waits for THIS process (by PID) to exit,
        2. copies the new exe over the current one (retrying while it's locked),
        3. relaunches it,
        4. deletes itself.

    Returns ``(ok, error)``. On success the caller must quit promptly so the
    helper can proceed. Only valid for a frozen build."""
    if not is_frozen():
        return (False, "Self-replace only works on the packaged LCEStudio.exe")
    target = os.path.abspath(target or current_exe())
    new_exe = os.path.abspath(new_exe)
    if not os.path.exists(new_exe):
        return (False, "Downloaded update not found")
    if os.name != "nt":
        return (False, "Automatic install is only supported on Windows")

    import tempfile
    pid = os.getpid()
    relaunch_line = 'start "" "%s"' % target if relaunch else "rem no relaunch"
    # Wait on the PID (not the image name — the relaunched app is LCEStudio.exe
    # too). Retry the copy for a few seconds in case the file is still locked.
    script = r"""@echo off
setlocal enableextensions
:waitproc
tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
    ping -n 2 127.0.0.1 >nul
    goto waitproc
)
set /a tries=0
:copyloop
copy /y "{new}" "{target}" >nul 2>&1
if not errorlevel 1 goto done
set /a tries+=1
if %tries% geq 30 goto giveup
ping -n 2 127.0.0.1 >nul
goto copyloop
:done
del /q "{new}" >nul 2>&1
{relaunch}
goto selfdestruct
:giveup
{relaunch}
:selfdestruct
del /q "%~f0" >nul 2>&1
""".format(pid=pid, new=new_exe, target=target, relaunch=relaunch_line)

    try:
        fd, bat = tempfile.mkstemp(prefix="LCEStudio-update-", suffix=".bat")
        with os.fdopen(fd, "w", encoding="ascii", errors="replace") as f:
            f.write(script)
    except Exception as e:
        return (False, "Could not stage the update helper: %s" % e)

    try:
        DETACHED = 0x00000008         # DETACHED_PROCESS
        NO_WINDOW = 0x08000000        # CREATE_NO_WINDOW
        NEW_GROUP = 0x00000200        # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            ["cmd", "/c", bat],
            creationflags=DETACHED | NO_WINDOW | NEW_GROUP,
            close_fds=True,
        )
        return (True, None)
    except Exception as e:
        try:
            os.remove(bat)
        except OSError:
            pass
        return (False, "Could not launch the update helper: %s" % e)
