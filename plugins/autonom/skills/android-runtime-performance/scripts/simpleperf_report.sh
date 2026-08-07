#!/usr/bin/env bash
# Produce self-time and inclusive Simpleperf reports from one perf.data capture.
set -euo pipefail

print_usage() {
  cat >&2 <<'EOF'
Usage: simpleperf_report.sh PERF_DATA [OUTPUT_DIR] [--first-party-regex REGEX]

Locates a host simpleperf binary (PATH, then common Android SDK/NDK roots) and
writes self-time and inclusive reports next to the capture (or under OUTPUT_DIR).
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  exit 0
fi

perf_data="${1:-}"
if [[ -z "$perf_data" ]]; then
  print_usage
  exit 2
fi
shift

output_dir=""
first_party_re=""
if (($# > 0)) && [[ "$1" != --* ]]; then
  output_dir="$1"
  shift
fi
while (($# > 0)); do
  case "$1" in
    --first-party-regex)
      first_party_re="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      print_usage
      exit 2
      ;;
  esac
done

if [[ ! -f "$perf_data" ]]; then
  echo "perf.data not found: $perf_data" >&2
  exit 1
fi

perf_data="$(cd "$(dirname "$perf_data")" && pwd)/$(basename "$perf_data")"
output_dir="${output_dir:-$(dirname "$perf_data")}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"

locate_simpleperf() {
  local candidate root
  candidate="$(command -v simpleperf || true)"
  if [[ -n "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  local -a roots=(
    "${ANDROID_NDK_HOME:-}"
    "${ANDROID_SDK_ROOT:-}"
    "${ANDROID_HOME:-}"
    "${HOME}/Library/Android/sdk"
  )
  for root in "${roots[@]}"; do
    [[ -n "$root" && -d "$root" ]] || continue
    candidate="$(find "$root" -type f -name simpleperf -perm -111 2>/dev/null | head -n 1 || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

simpleperf_bin="$(locate_simpleperf || true)"
if [[ -z "$simpleperf_bin" ]]; then
  echo "simpleperf not found on PATH or under Android SDK/NDK roots" >&2
  exit 1
fi

self_report="${output_dir}/simpleperf-self.txt"
inclusive_report="${output_dir}/simpleperf-inclusive.txt"

"$simpleperf_bin" report -i "$perf_data" >"$self_report"
"$simpleperf_bin" report -i "$perf_data" --children >"$inclusive_report"
printf 'Wrote: %s\nWrote: %s\n' "$self_report" "$inclusive_report"

if [[ -n "$first_party_re" ]]; then
  filtered="${output_dir}/simpleperf-first-party.txt"
  grep -E "$first_party_re" "$inclusive_report" >"$filtered" || true
  printf 'Wrote: %s\n' "$filtered"
fi
