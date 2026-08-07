#!/usr/bin/env bash
# Capture a single Android process memory evidence pack (meminfo + optional hprof).
set -euo pipefail

print_usage() {
  cat >&2 <<'EOF'
Usage: capture_android_memory.sh --package PACKAGE --out-dir DIR [options]

Options:
  --serial SERIAL   Explicit adb target (required when more than one device is up).
  --label LABEL     Artifact label prefix (default: capture).
  --no-hprof        Do not request a Java/Kotlin heap dump.
EOF
}

serial=""
package=""
out_dir=""
label="capture"
want_hprof=1

while (($# > 0)); do
  case "$1" in
    --serial)
      serial="${2:-}"
      shift 2
      ;;
    --package)
      package="${2:-}"
      shift 2
      ;;
    --out-dir)
      out_dir="${2:-}"
      shift 2
      ;;
    --label)
      label="${2:-}"
      shift 2
      ;;
    --no-hprof)
      want_hprof=0
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      print_usage
      exit 2
      ;;
  esac
done

if [[ -z "$package" || -z "$out_dir" ]]; then
  print_usage
  exit 2
fi

if ! command -v adb >/dev/null 2>&1; then
  echo "adb is not installed or not on PATH" >&2
  exit 1
fi

if [[ -z "$serial" ]]; then
  mapfile -t devices < <(adb devices | awk 'NR > 1 && $2 == "device" { print $1 }')
  if ((${#devices[@]} == 1)); then
    serial="${devices[0]}"
  elif ((${#devices[@]} == 0)); then
    echo "No authorized adb device is connected." >&2
    exit 1
  else
    echo "Multiple adb devices are connected; pass --serial." >&2
    adb devices -l >&2
    exit 1
  fi
fi

sanitize() {
  printf '%s' "$1" | tr -cs 'A-Za-z0-9._-' '_'
}

safe_label="$(sanitize "$label")"
safe_package="$(sanitize "$package")"
mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
prefix="${out_dir}/${safe_label}-${stamp}"
remote_hprof="/data/local/tmp/${safe_package}-${stamp}.hprof"

cleanup_remote() {
  adb -s "$serial" shell rm -f "$remote_hprof" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT INT TERM

pid="$(adb -s "$serial" shell pidof -s "$package" | tr -d '\r')"
if [[ -z "$pid" ]]; then
  echo "App process is not running: $package" >&2
  exit 1
fi

{
  printf 'captured_at_utc=%s\n' "$stamp"
  printf 'serial=%s\n' "$serial"
  printf 'package=%s\n' "$package"
  printf 'pid=%s\n' "$pid"
  adb -s "$serial" shell getprop ro.build.version.release | tr -d '\r' | sed 's/^/android_release=/'
  adb -s "$serial" shell getprop ro.build.version.sdk | tr -d '\r' | sed 's/^/android_sdk=/'
} >"${prefix}-metadata.txt"

adb -s "$serial" shell dumpsys meminfo "$package" >"${prefix}-meminfo.txt"
adb -s "$serial" shell cat "/proc/${pid}/status" >"${prefix}-proc-status.txt"
adb -s "$serial" shell dumpsys gfxinfo "$package" >"${prefix}-gfxinfo.txt" || true

if ((want_hprof)); then
  if adb -s "$serial" shell am dumpheap -g "$package" "$remote_hprof"; then
    adb -s "$serial" pull "$remote_hprof" "${prefix}.hprof" >/dev/null
  else
    echo "Heap dump failed; retry with a debuggable build or pass --no-hprof." >&2
    exit 1
  fi
fi

printf 'Wrote artifacts with prefix: %s\n' "$prefix"
