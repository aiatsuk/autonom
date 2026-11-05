"""Build the on-device Android helper on first use (AH-01, AH-02).

The helper ships as Java source only (``native/android/helper/src``); no
``.dex``, ``.jar`` or ``.class`` file is committed or released. The first call
that needs it compiles it with the local JDK and Android SDK:

    javac -Xlint:-options -encoding UTF-8 --release 8 -cp <android.jar> -d <tmp>/classes <sources>
    d8 --lib <android.jar> --min-api 26 --output <tmp>/dex <classes>

Tools:

- SDK roots, in order: ``$ANDROID_HOME``, ``$ANDROID_SDK_ROOT``, two levels
  above the adb realpath (as ``emulator.find_emulator`` derives it),
  ``~/Library/Android/sdk``, ``~/Android/Sdk``.
- ``d8``: the newest ``build-tools/<version>/d8`` of the first root that has
  one (never a ``d8`` found on PATH, such as Homebrew's).
- ``android.jar``: the newest ``platforms/android-<version>/android.jar`` of
  the first root that has one, in numeric order ("android-37.0" names too).
- ``javac``: ``$JAVA_HOME/bin/javac``, else the first ``javac`` on PATH. Its
  JDK ``bin`` goes first on PATH for both tools: the SDK's ``d8`` script runs
  ``java`` from PATH. A ``javac`` whose realpath has no JDK around it (no
  ``release`` file or ``lib/modules`` next to its ``bin``) is a launcher that
  picks a JDK each time it runs: the macOS ``/usr/bin/javac`` (looked through
  with ``/usr/libexec/java_home`` when ``JAVA_HOME`` is unset), jenv or asdf
  shims.

The build id is the first 16 hex digits of a SHA-256 over the protocol
number, the build recipe, every source path and content, the ``javac
-version`` and ``d8 --version`` output, and the android.jar path, size and
mtime. A finished build is ``<state>/android-helper/builds/<id>/classes.dex``
with a ``complete`` marker, made in a temporary sibling directory and renamed
into place under an exclusive ``flock`` on ``<state>/android-helper/build.lock``,
so concurrent first uses build once and nobody sees a partial build. A failed
build writes ``builds/<id>.failed`` (the tool's output tail) and that id is not
built again; new sources or tools give a new id. ``<state>`` is the Autonom
state base: ``$AUTONOM_HOME``, else ``$XDG_STATE_HOME/autonom``, else
``~/.local/state/autonom`` (the rule of ``control.py``), never the install tree.

Tool versions are asked once per tool file (path, size and mtime of the
tool and of its JDK's ``release`` and ``lib/modules`` are the key, kept in
``<state>/android-helper/tool-versions.json``), so a warm call costs a few
``stat`` calls and one hash of the sources, not a JVM start. A launcher
``javac`` is asked on every call instead: the JDK behind it can change while
its own file stays the same, and the build id must follow the JDK.

``ensure`` never raises for a missing tool, a failed build or a state
directory it cannot write: the result names the reason and a hint.

Only light standard-library modules are imported at module level: this module
sits on the path of quick verbs such as ``ui tree``.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterator, Mapping, NamedTuple

PROTOCOL = 1
HELPER_DIR = Path(__file__).resolve().parents[2] / "native" / "android" / "helper"
MIN_API = 26
JAVA_RELEASE = "8"
# Each tool run of a build gets this long; a version probe gets VERSION_TIMEOUT.
TOOL_TIMEOUT = 120
VERSION_TIMEOUT = 60
# The flags are part of the build id: changing them makes a new build.
RECIPE = f"javac --release {JAVA_RELEASE} -encoding UTF-8; d8 --min-api {MIN_API}"
FAILURE_TAIL = 4000
VERSION_CACHE_ENTRIES = 32
JAVA_HOME_TIMEOUT = 10

# The macOS launcher on PATH and the tool that names the JDK it runs.
IS_MACOS = sys.platform == "darwin"
MACOS_JAVAC_LAUNCHER = Path("/usr/bin/javac")
MACOS_JAVA_HOME = Path("/usr/libexec/java_home")

ROOT_DIR = "android-helper"
BUILDS_DIR = "builds"
LOCK_FILE = "build.lock"
VERSIONS_FILE = "tool-versions.json"
COMPLETE = "complete"
DEX = "classes.dex"

HINT_JAVAC = ("Install a JDK (11 or newer) and put javac on PATH or set JAVA_HOME; "
              "until then Android calls use uiautomator and adb input.")
HINT_D8 = ("Install Android SDK build-tools (sdkmanager \"build-tools;36.0.0\") or set "
           "ANDROID_HOME to the SDK; until then Android calls use uiautomator and adb input.")
HINT_JAR = ("Install an Android SDK platform (sdkmanager \"platforms;android-36\") or set "
            "ANDROID_HOME to the SDK; until then Android calls use uiautomator and adb input.")


class Toolchain(NamedTuple):
    javac: Path
    java_bin: Path
    d8: Path
    android_jar: Path
    javac_version: str
    d8_version: str


class Build(NamedTuple):
    """What `ensure` found: a dex to push, or the reason there is none."""

    dex: Path | None
    build_id: str | None
    reason: str | None
    hint: str | None
    built: bool = False
    toolchain: Toolchain | None = None

    @property
    def ok(self) -> bool:
        return self.dex is not None


class _ToolFailed(Exception):
    pass


# --- Layout ---------------------------------------------------------------------

def _env(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def state_base(env: Mapping[str, str] | None = None) -> Path:
    """`$AUTONOM_HOME`, else `$XDG_STATE_HOME/autonom`, else
    `~/.local/state/autonom` (control.py's state base)."""
    values = _env(env)
    explicit = values.get("AUTONOM_HOME")
    if explicit:
        return Path(explicit)
    state = values.get("XDG_STATE_HOME")
    return Path(state) / "autonom" if state else _home(values) / ".local/state/autonom"


def helper_root(env: Mapping[str, str] | None = None) -> Path:
    return state_base(env) / ROOT_DIR


# --- Discovery ------------------------------------------------------------------

_VERSION = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")


def version_key(name: str) -> tuple:
    """Order of SDK directory versions: numeric parts first ("37.0" > "36"
    > "9"), a release above its previews ("36.0.0" > "36.0.0-rc1"), and
    names without a number below every numbered one."""
    match = _VERSION.match(name)
    if not match:
        return (0, (), False, name)
    numbers = tuple(int(part) for part in match.group(1).split("."))
    suffix = match.group(2)
    return (1, numbers, suffix == "", suffix)


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def sdk_roots(env: Mapping[str, str] | None = None, *, adb_path: str | None = None) -> list[Path]:
    """Existing SDK roots in discovery order, each once."""
    values = _env(env)
    candidates: list[Path] = []
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if values.get(name):
            candidates.append(Path(values[name]))
    adb = adb_path or values.get("AUTONOM_ADB") or shutil.which("adb", path=values.get("PATH", ""))
    if adb:
        candidates.append(Path(adb).resolve().parent.parent)
    home = _home(values)
    candidates += [home / "Library/Android/sdk", home / "Android/Sdk"]
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        key = os.path.realpath(candidate)
        if key not in seen:
            seen.add(key)
            roots.append(candidate)
    return roots


def find_d8(roots: list[Path]) -> Path | None:
    for root in roots:
        found = [path for path in (root / "build-tools").glob("*/d8") if _executable(path)]
        if found:
            return max(found, key=lambda path: version_key(path.parent.name))
    return None


def find_android_jar(roots: list[Path]) -> Path | None:
    for root in roots:
        found = [path for path in (root / "platforms").glob("android-*/android.jar") if path.is_file()]
        if found:
            return max(found, key=lambda path: version_key(path.parent.name[len("android-"):]))
    return None


def jdk_home(javac: Path) -> Path | None:
    """The JDK around `javac`'s realpath (it has a `release` file or
    `lib/modules`), or None for a launcher that picks a JDK when it runs."""
    home = javac.resolve().parent.parent
    if (home / "release").is_file() or (home / "lib" / "modules").is_file():
        return home
    return None


def find_javac(env: Mapping[str, str] | None = None) -> Path | None:
    values = _env(env)
    java_home = values.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / "javac"
        if _executable(candidate):
            return candidate
    on_path = shutil.which("javac", path=values.get("PATH", ""))
    if not on_path:
        return None
    javac = Path(on_path)
    if not java_home and jdk_home(javac) is None:
        return _macos_jdk_javac(javac, values) or javac
    return javac


def _macos_jdk_javac(javac: Path, env: Mapping[str, str]) -> Path | None:
    """For the macOS `/usr/bin/javac` launcher: the javac of the JDK it would
    run, as `/usr/libexec/java_home` names it; None anywhere else, or when
    no JDK is named."""
    if not IS_MACOS or os.path.realpath(javac) != os.path.realpath(MACOS_JAVAC_LAUNCHER):
        return None
    if not _executable(MACOS_JAVA_HOME):
        return None
    try:
        completed = _run([str(MACOS_JAVA_HOME)], env, JAVA_HOME_TIMEOUT)
    except _ToolFailed:
        return None
    lines = (completed.stdout or "").strip().splitlines()
    if completed.returncode or not lines:
        return None
    candidate = Path(lines[-1].strip()) / "bin" / "javac"
    if _executable(candidate) and jdk_home(candidate) is not None:
        return candidate
    return None


def java_bin(javac: Path) -> Path:
    """The JDK `bin` directory of `javac`, where its `java` lives."""
    real = javac.resolve().parent
    return real if (real / "java").exists() else javac.parent


# --- Tool versions ----------------------------------------------------------------

def _stat(path: Path) -> list:
    try:
        info = path.stat()
        return [str(path), info.st_size, info.st_mtime_ns]
    except OSError:
        return [str(path), None, None]


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _tool_env(env: Mapping[str, str], bin_dir: Path) -> dict[str, str]:
    values = dict(env)
    values["PATH"] = str(bin_dir) + (os.pathsep + values["PATH"] if values.get("PATH") else "")
    return values


def _run(argv: list[str], env: Mapping[str, str], timeout: float, cwd: Path | None = None
         ) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, env=dict(env), cwd=cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, errors="replace",
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _ToolFailed(f"{Path(argv[0]).name} did not finish within {int(timeout)} s") from exc
    except OSError as exc:
        raise _ToolFailed(f"{Path(argv[0]).name} could not start: {exc}") from exc


def _output(completed: subprocess.CompletedProcess) -> str:
    return "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())


def tool_version(argv: list[str], identity: list | None, env: Mapping[str, str],
                 cache: Path) -> str:
    """`argv`'s version text, asked once per tool identity (path, size, mtime);
    asked every time when there is no identity to key it (a launcher)."""
    key = None if identity is None else hashlib.sha256(json.dumps([argv, identity]).encode()).hexdigest()
    known = _read_json(cache) if key else {}
    if key and isinstance(known.get(key), str):
        return known[key]
    completed = _run(argv, env, VERSION_TIMEOUT)
    text = _output(completed)
    if completed.returncode or not text:
        command = " ".join([Path(argv[0]).name, *argv[1:]])
        raise _ToolFailed(f"`{command}` failed (exit {completed.returncode}): {text[-300:]}")
    if key is None:
        return text
    known[key] = text
    while len(known) > VERSION_CACHE_ENTRIES:
        known.pop(next(iter(known)))
    try:
        _write_json(cache, known)
    except OSError:
        pass  # asked again next time
    return text


def find_toolchain(env: Mapping[str, str] | None = None, *, adb_path: str | None = None,
                   cache: Path | None = None) -> Toolchain | Build:
    """The toolchain, or a failed `Build` naming the missing or broken tool."""
    values = _env(env)
    cache = cache or helper_root(values) / VERSIONS_FILE
    javac = find_javac(values)
    if javac is None:
        return Build(None, None, "javac not found", HINT_JAVAC)
    roots = sdk_roots(values, adb_path=adb_path)
    d8 = find_d8(roots)
    if d8 is None:
        return Build(None, None, "d8 not found", HINT_D8)
    jar = find_android_jar(roots)
    if jar is None:
        return Build(None, None, "android.jar not found", HINT_JAR)
    bin_dir = java_bin(javac)
    tools = _tool_env(values, bin_dir)
    jdk = jdk_home(javac)
    # A launcher has no file that changes with the JDK behind it: no key.
    identity = None if jdk is None else [
        _stat(javac.resolve()), _stat(jdk / "release"), _stat(jdk / "lib" / "modules")]
    try:
        javac_version = tool_version([str(javac), "-version"], identity, tools, cache)
    except _ToolFailed as exc:
        return Build(None, None, f"javac does not run: {exc}", HINT_JAVAC)
    try:
        d8_version = tool_version(
            [str(d8), "--version"],
            [_stat(d8.resolve()), _stat(d8.parent / "lib" / "d8.jar"), _stat(d8.parent / "d8.jar"),
             _stat((bin_dir / "java").resolve())],
            tools, cache)
    except _ToolFailed as exc:
        return Build(None, None, f"d8 does not run: {exc}", HINT_D8)
    return Toolchain(javac, bin_dir, d8, jar, javac_version, d8_version)


# --- Build id and cache -------------------------------------------------------------

def source_files(source_dir: Path | None = None) -> list[Path]:
    root = (source_dir or HELPER_DIR) / "src"
    return sorted(root.rglob("*.java"))


def build_id(toolchain: Toolchain, source_dir: Path | None = None) -> str:
    base = source_dir or HELPER_DIR
    digest = hashlib.sha256()

    def feed(label: str, value: bytes) -> None:
        digest.update(label.encode() + b"\0" + len(value).to_bytes(8, "big") + value)

    feed("protocol", str(PROTOCOL).encode())
    feed("recipe", RECIPE.encode())
    for path in source_files(base):
        feed("source", path.relative_to(base).as_posix().encode())
        feed("content", path.read_bytes())
    feed("javac", toolchain.javac_version.encode())
    feed("d8", toolchain.d8_version.encode())
    jar = toolchain.android_jar.stat()
    feed("android.jar", f"{toolchain.android_jar}\0{jar.st_size}\0{jar.st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    import fcntl  # POSIX only; importing this module elsewhere needs no fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _complete(directory: Path) -> bool:
    return (directory / COMPLETE).is_file() and (directory / DEX).is_file()


def _marker_hint(marker: Path) -> str:
    return (f"See {marker} for the compiler output. Fix the JDK or SDK (a new javac, d8 or "
            f"android.jar builds again), or delete that file to retry; until then Android calls "
            f"use uiautomator and adb input.")


def _failed_earlier(identifier: str, marker: Path, toolchain: Toolchain) -> Build:
    try:
        first = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        first = "see the failure file"
    return Build(None, identifier, f"helper build failed earlier: {first}", _marker_hint(marker),
                 False, toolchain)


def _fail(identifier: str, marker: Path, toolchain: Toolchain, summary: str, output: str) -> Build:
    text = summary + "\n" + output[-FAILURE_TAIL:] + "\n"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(marker)
    except OSError:
        pass  # nothing to remember with; the next call tries again
    return Build(None, identifier, f"helper build failed: {summary}", _marker_hint(marker),
                 False, toolchain)


def _state_hint(root: Path) -> str:
    return (f"Check that {root} can be created and written (the Autonom state directory: "
            f"AUTONOM_HOME, else XDG_STATE_HOME/autonom, else ~/.local/state/autonom); until "
            f"then Android calls use uiautomator and adb input.")


def ensure(*, adb_path: str | None = None, env: Mapping[str, str] | None = None,
           source_dir: Path | None = None) -> Build:
    """The helper's dex for the current sources and toolchain, built when the
    cache has none. Never raises for a missing tool, a failed build or a state
    directory that cannot be written: the result names the reason and a hint
    instead."""
    values = _env(env)
    root = helper_root(values)
    try:
        return _ensure(values, root, adb_path, source_dir)
    except OSError as exc:  # a source or android.jar that cannot be read
        return Build(None, None, f"helper build failed: {exc}",
                     f"Check that the file named there can be read (the helper sources, "
                     f"android.jar, or the Autonom state directory {root}); until then Android "
                     f"calls use uiautomator and adb input.")


def _ensure(values: Mapping[str, str], root: Path, adb_path: str | None,
            source_dir: Path | None) -> Build:
    found = find_toolchain(values, adb_path=adb_path, cache=root / VERSIONS_FILE)
    if isinstance(found, Build):
        return found
    toolchain = found
    base = source_dir or HELPER_DIR
    if not source_files(base):
        return Build(None, None, "helper source not found",
                     f"Expected the Java sources under {base / 'src'}; reinstall Autonom.",
                     False, toolchain)
    identifier = build_id(toolchain, base)
    builds = root / BUILDS_DIR
    final = builds / identifier
    marker = builds / f"{identifier}.failed"
    if _complete(final):
        return Build(final / DEX, identifier, None, None, False, toolchain)
    if marker.exists():
        return _failed_earlier(identifier, marker, toolchain)
    try:
        with _lock(root / LOCK_FILE):
            # Another process may have finished while this one waited.
            if _complete(final):
                return Build(final / DEX, identifier, None, None, False, toolchain)
            if marker.exists():
                return _failed_earlier(identifier, marker, toolchain)
            return _build_locked(toolchain, base, identifier, builds, values)
    except OSError as exc:  # the state directory or its lock file cannot be made
        return Build(None, identifier, f"helper build failed: {exc}", _state_hint(root),
                     False, toolchain)


def _build_locked(toolchain: Toolchain, base: Path, identifier: str, builds: Path,
                  env: Mapping[str, str]) -> Build:
    import tempfile

    marker = builds / f"{identifier}.failed"
    final = builds / identifier
    work: Path | None = None
    try:
        builds.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Only a builder holding the lock makes temporary directories, so any
        # left now belongs to a build that was killed: never published, removed.
        for stale in builds.glob(".tmp-*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
        work = Path(tempfile.mkdtemp(prefix=f".tmp-{identifier}-", dir=builds))
        classes = work / "classes"
        out = work / "dex"
        classes.mkdir()
        out.mkdir()  # d8 writes only into an existing directory
        tools = _tool_env(env, toolchain.java_bin)
        jar = str(toolchain.android_jar)
        javac = [str(toolchain.javac), "-Xlint:-options", "-encoding", "UTF-8",
                 "--release", JAVA_RELEASE, "-cp", jar, "-d", str(classes),
                 *(str(path) for path in source_files(base))]
        try:
            completed = _run(javac, tools, TOOL_TIMEOUT, cwd=work)
        except _ToolFailed as exc:
            return _fail(identifier, marker, toolchain, str(exc), "")
        if completed.returncode:
            return _fail(identifier, marker, toolchain, f"javac exited {completed.returncode}",
                         _output(completed))
        compiled = sorted(str(path) for path in classes.rglob("*.class"))
        if not compiled:
            return _fail(identifier, marker, toolchain, "javac wrote no class files", _output(completed))
        d8 = [str(toolchain.d8), "--lib", jar, "--min-api", str(MIN_API), "--output", str(out),
              *compiled]
        try:
            completed = _run(d8, tools, TOOL_TIMEOUT, cwd=work)
        except _ToolFailed as exc:
            return _fail(identifier, marker, toolchain, str(exc), "")
        if completed.returncode:
            return _fail(identifier, marker, toolchain, f"d8 exited {completed.returncode}",
                         _output(completed))
        produced = out / DEX
        if not produced.is_file() or produced.stat().st_size == 0:
            return _fail(identifier, marker, toolchain, "d8 wrote no classes.dex", _output(completed))
        produced.replace(work / DEX)
        shutil.rmtree(classes)
        shutil.rmtree(out)
        (work / "build.json").write_text(json.dumps({
            "build": identifier, "protocol": PROTOCOL, "recipe": RECIPE,
            "javac": str(toolchain.javac), "javac_version": toolchain.javac_version,
            "d8": str(toolchain.d8), "d8_version": toolchain.d8_version,
            "android_jar": jar,
        }, indent=2) + "\n", encoding="utf-8")
        (work / COMPLETE).write_text(identifier + "\n", encoding="utf-8")
        if final.exists():
            shutil.rmtree(final)  # an unfinished directory from an older layout or a crash
        os.rename(work, final)
        work = None
        return Build(final / DEX, identifier, None, None, True, toolchain)
    except OSError as exc:
        return Build(None, identifier, f"helper build failed: {exc}",
                     f"Check that {builds} is writable; until then Android calls use uiautomator "
                     f"and adb input.", False, toolchain)
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def dex_or_reason(*, adb_path: str | None = None, env: Mapping[str, str] | None = None
                  ) -> tuple[Path | None, str | None]:
    """The dex path, or why there is none (a short reason for `fallback_reason`)."""
    result = ensure(adb_path=adb_path, env=env)
    return result.dex, result.reason


def probe(*, adb_path: str | None = None, env: Mapping[str, str] | None = None) -> dict:
    """What a build would use, and whether it is cached, without building."""
    values = _env(env)
    root = helper_root(values)
    found = find_toolchain(values, adb_path=adb_path, cache=root / VERSIONS_FILE)
    if isinstance(found, Build):
        return {"tools": False, "reason": found.reason, "hint": found.hint, "build": None,
                "cached": False}
    identifier = build_id(found)
    return {
        "tools": True, "javac": str(found.javac), "d8": str(found.d8),
        "android_jar": str(found.android_jar), "build": identifier,
        "cached": _complete(root / BUILDS_DIR / identifier),
        "failed": (root / BUILDS_DIR / f"{identifier}.failed").exists(),
    }
