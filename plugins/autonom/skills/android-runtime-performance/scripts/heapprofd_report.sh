#!/usr/bin/env bash
# Extract heapprofd allocation summaries from a Perfetto .pftrace capture.
set -euo pipefail

print_usage() {
  echo "Usage: heapprofd_report.sh TRACE.pftrace [OUTPUT_DIR]" >&2
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  exit 0
fi

trace="${1:-}"
if [[ ! -f "$trace" ]]; then
  print_usage
  echo "Trace not found: ${trace:-<missing>}" >&2
  exit 2
fi

trace="$(cd "$(dirname "$trace")" && pwd)/$(basename "$trace")"
output_dir="${2:-$(dirname "$trace")}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"

processor="${TRACE_PROCESSOR_SHELL:-}"
if [[ -z "$processor" ]]; then
  processor="$(command -v trace_processor_shell || command -v trace_processor || true)"
fi
if [[ -z "$processor" ]]; then
  echo "Perfetto trace processor not found; set TRACE_PROCESSOR_SHELL" >&2
  exit 1
fi

"$processor" -Q '
select count(*) as allocation_rows,
       coalesce(sum(size), 0) as net_size_bytes,
       coalesce(sum(count), 0) as net_count
from __intrinsic_heap_profile_allocation;
' "$trace" >"${output_dir}/heapprofd-summary.txt"

"$processor" -Q '
with alloc as (
  select callsite_id, sum(size) as net_size_bytes, sum(count) as net_count
  from __intrinsic_heap_profile_allocation
  group by callsite_id
  having sum(size) > 0
)
select alloc.callsite_id, alloc.net_size_bytes, alloc.net_count,
       frame.name as leaf_frame, mapping.name as leaf_mapping
from alloc
join __intrinsic_stack_profile_callsite callsite on callsite.id = alloc.callsite_id
join __intrinsic_stack_profile_frame frame on frame.id = callsite.frame_id
join __intrinsic_stack_profile_mapping mapping on mapping.id = frame.mapping
order by alloc.net_size_bytes desc
limit 50;
' "$trace" >"${output_dir}/heapprofd-top-allocations.txt"

"$processor" -Q "
select name, idx, severity, source, value, description
from stats
where lower(name) like '%heapprofd%'
   or lower(name) like '%packet_loss%'
   or lower(name) like '%overrun%'
order by name, idx;
" "$trace" >"${output_dir}/heapprofd-health.txt"

printf 'Wrote reports to: %s\n' "$output_dir"
