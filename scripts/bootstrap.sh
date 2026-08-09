#!/usr/bin/env bash
# Install the device tools Autonom drives — the ones the bundle cannot carry.
#
# Two classes, kept apart on purpose (FLARE-130's lesson):
#   mechanical — a package manager can install it unattended (adb, mitmproxy, idb)
#   human      — needs a person: Xcode from the App Store, an Android AVD, a login
#
# Read-only by default: it reports what is missing and the exact command to fix
# each, and exits non-zero if anything mechanical is missing. `--install` runs the
# mechanical class; the human class is only ever printed. Idempotent — a tool
# already present is left alone.
set -euo pipefail

DO_INSTALL=0
[ "${1:-}" = "--install" ] && DO_INSTALL=1

os="$(uname -s)"
missing_mechanical=0

have() { command -v "$1" >/dev/null 2>&1; }

# report <name> <present?> <install-command>
report() {
  local name="$1" present="$2" cmd="$3"
  if [ "$present" = "1" ]; then
    printf '  [ok]   %s\n' "$name"
    return
  fi
  printf '  [MISS] %s\n' "$name"
  printf '         %s\n' "$cmd"
}

run() {
  echo "  + $*"
  "$@"
}

echo "Autonom device tools ($os):"

# --- adb (Android) ---------------------------------------------------------
if have adb; then
  report adb 1 ""
else
  missing_mechanical=1
  if [ "$os" = "Darwin" ]; then
    cmd="brew install android-platform-tools"
  else
    cmd="sudo apt-get install -y android-tools-adb"
  fi
  report adb 0 "$cmd"
  if [ "$DO_INSTALL" = "1" ]; then
    if [ "$os" = "Darwin" ] && have brew; then run brew install android-platform-tools
    elif have apt-get; then run sudo apt-get install -y android-tools-adb
    else echo "         no supported package manager; install adb manually" >&2; fi
  fi
fi

# --- mitmproxy (network) ---------------------------------------------------
if have mitmdump; then
  report mitmproxy 1 ""
else
  missing_mechanical=1
  if [ "$os" = "Darwin" ] && have brew; then cmd="brew install mitmproxy"
  else cmd="pipx install mitmproxy   # or: python3 -m pip install --user mitmproxy"; fi
  report mitmproxy 0 "$cmd"
  if [ "$DO_INSTALL" = "1" ]; then
    if [ "$os" = "Darwin" ] && have brew; then run brew install mitmproxy
    elif have pipx; then run pipx install mitmproxy
    else echo "         install pipx first, then 'pipx install mitmproxy'" >&2; fi
  fi
fi

# --- idb (iOS UI; macOS only) ----------------------------------------------
if [ "$os" = "Darwin" ]; then
  if have idb; then
    report idb 1 ""
  else
    missing_mechanical=1
    # `brew trust` is not optional: Homebrew refuses to load a formula from a
    # third-party tap without it, and the resulting error names neither the tap
    # nor the fix. Leaving it out meant --install reliably tapped, then failed.
    # `--formula` keeps the grant to this one formula rather than the whole tap.
    report idb 0 "brew tap facebook/fb && brew trust --formula facebook/fb/idb-companion && brew install idb-companion && pipx install fb-idb"
    if [ "$DO_INSTALL" = "1" ] && have brew; then
      run brew tap facebook/fb
      run brew trust --formula facebook/fb/idb-companion
      run brew install idb-companion
      have pipx && run pipx install fb-idb || echo "         then: pipx install fb-idb" >&2
    fi
  fi
fi

# --- human class (never auto) ----------------------------------------------
echo
echo "Human steps (not automated):"
if [ "$os" = "Darwin" ] && ! have xcrun; then
  echo "  - Xcode + iOS runtime: App Store -> Xcode, then 'xcodebuild -downloadPlatform iOS'"
fi
echo "  - Android emulator (AVD): Android Studio, or sdkmanager + avdmanager (google_apis image)"
echo "  - Trust the MITM CA on a device: 'autonom network attach --i-understand-mitm --install-ca'"
echo "    (per device, explicit consent — by design)"

echo
if [ "$missing_mechanical" = "1" ] && [ "$DO_INSTALL" = "0" ]; then
  echo "Mechanical tools missing. Re-run with --install to fetch them."
  exit 1
fi
echo "Bootstrap check complete."
