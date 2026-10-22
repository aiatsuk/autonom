"""System language for the explicitly selected device; never per-app overrides."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from . import adb, errors, ios_prefs, ios_simctl

TAG = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
# Runs as the existing adb shell user. No APK, signing, root or permissions changes.
ANDROID_SOURCE = """
import android.content.res.Configuration;
import android.os.LocaleList;
public final class AutonomLocale {
  public static void main(String[] args) throws Exception {
    Object service = Class.forName("android.app.ActivityManager").getMethod("getService").invoke(null);
    Class<?> api = Class.forName("android.app.IActivityManager");
    Configuration config = (Configuration) api.getMethod("getConfiguration").invoke(service);
    if (args.length == 1) {
      config.setLocales(LocaleList.forLanguageTags(args[0]));
      Configuration.class.getField("userSetLocale").setBoolean(config, true);
      api.getMethod("updatePersistentConfiguration", Configuration.class).invoke(service, config);
    }
    config = (Configuration) api.getMethod("getConfiguration").invoke(service);
    System.out.println(config.getLocales().toLanguageTags());
  }
}
"""

def checked_tag(value):
    if not isinstance(value, str) or len(value) > 48 or not TAG.fullmatch(value):
        raise errors.AutonomError(errors.INVALID_VALUE, "Choose a valid language tag")
    return value

def android_call(target, args):
    return adb.run_adb(target.tool, args, serial=target.target_id, timeout=30).stdout.strip()

def current(target):
    if target.platform == "android":
        languages = android_call(target, ["shell", "settings", "get", "system", "system_locales"])
        if languages in ("", "null"):
            languages = android_call(target, ["shell", "getprop", "persist.sys.locale"])
        if not languages:
            languages = android_call(target, ["shell", "getprop", "ro.product.locale"])
        language = languages.split(",")[0].replace("_", "-")
    else:
        result = ios_simctl.run_simctl(target.tool, ["spawn", target.target_id, "defaults", "export", "NSGlobalDomain", "-"])
        values = plistlib.loads(result.stdout.encode())
        languages = values.get("AppleLanguages") or []
        language = languages[0] if languages else ""
    if not isinstance(language, str) or not TAG.fullmatch(language):
        raise errors.AutonomError(errors.BACKEND_FAILED, "The device did not report its current language")
    return {"language": language, "restart_required": target.platform == "ios"}

def android_helper(target):
    roots = [Path(target.tool).resolve().parent.parent]
    roots += [Path(os.environ[k]).expanduser() for k in ("ANDROID_SDK_ROOT", "ANDROID_HOME") if os.environ.get(k)]
    roots += [Path.home()/"Library/Android/sdk", Path.home()/"Android/Sdk"]
    def version(path):
        return tuple(int(n) for n in re.findall(r"\d+", str(path)))
    root = next((r for r in roots if list(r.glob("build-tools/*/d8")) and list(r.glob("platforms/*/android.jar"))), None)
    javac = shutil.which("javac")
    if root is None or javac is None:
        raise errors.AutonomError(errors.UNSUPPORTED_CAPABILITY, "Changing Android language needs the local Android SDK and JDK", "Change the language in Android Settings.")
    jar = max(root.glob("platforms/*/android.jar"), key=version)
    d8 = max(root.glob("build-tools/*/d8"), key=version)
    key = hashlib.sha256((ANDROID_SOURCE+str(jar)+str(d8)).encode()).hexdigest()[:16]
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home()/".cache"))/"autonom/device-language"/key
    dex = cache/"classes.dex"
    if not dex.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as work:
            work = Path(work)
            source = work/"AutonomLocale.java"; source.write_text(ANDROID_SOURCE)
            subprocess.run([javac, "--release", "8", "-classpath", str(jar), "-d", str(work), str(source)],
                           check=True, capture_output=True, timeout=60)
            subprocess.run([str(d8), "--lib", str(jar), "--min-api", "26", "--output", str(work), str(work/"AutonomLocale.class")],
                           check=True, capture_output=True, timeout=60)
            (work/"classes.dex").replace(dex)
    return dex, "/data/local/tmp/autonom-locale-"+key+".dex"

def set_language(target, language, *, restart=False):
    language = checked_tag(language)
    before = current(target)
    if before["language"] == language:
        return {**before, "verified": True, "restarted": False}
    if target.platform == "android":
        try:
            dex, remote = android_helper(target)
            android_call(target, ["push", str(dex), remote])
            try:
                android_call(target, ["shell", "CLASSPATH="+remote, "app_process", "/", "AutonomLocale", language])
            finally:
                android_call(target, ["shell", "rm", "-f", remote])
            for _ in range(10):
                observed = current(target)
                if observed["language"] == language:
                    return {**observed, "verified": True, "restarted": False}
                time.sleep(.2)
        except (OSError, subprocess.SubprocessError) as exc:
            raise errors.AutonomError(errors.BACKEND_FAILED, "Could not change Android system language", "Check the Android SDK/JDK installation.") from exc
    else:
        if restart is not True:
            raise errors.AutonomError(errors.INVALID_VALUE, "Changing the Simulator language requires a restart")
        ios_prefs.require_preferences_dir(target.target_id)
        device = ios_simctl.find_simulator(target.tool, target.target_id)
        if device is None or device.state != "Booted":
            raise errors.AutonomError(errors.BACKEND_FAILED, "Wait until the Simulator is booted")
        ios_simctl.shutdown(target.tool, target.target_id)
        device = ios_simctl.find_simulator(target.tool, target.target_id)
        if device is None or device.state != "Shutdown":
            raise errors.AutonomError(errors.BACKEND_FAILED, "Simulator did not shut down; language was not changed")
        try:
            values = ios_prefs.read_domain(target.target_id, ios_prefs.GLOBAL_DOMAIN)
            existing = values.get("AppleLanguages") or []
            values["AppleLanguages"] = [language, *[v for v in existing if v != language]]
            ios_prefs.write_domain(target.target_id, ios_prefs.GLOBAL_DOMAIN, values)
        finally:
            ios_simctl.boot(target.tool, target.target_id)
        observed = current(target)
        if observed["language"] == language:
            return {**observed, "verified": True, "restarted": True}
    raise errors.AutonomError(errors.BACKEND_FAILED, "The device did not confirm the selected language", "Reopen Device settings to read its current language.")
