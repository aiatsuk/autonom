// Helpers the live Canvas scripts share for what goes into their reports.

/**
 * `text` with every occurrence of each of `words` (any case) replaced by "[typed text]", so
 * CLI output recorded after a test typed them (a `ui tree` error that echoes the screen)
 * never carries the typed words into a report.
 */
export function withoutTypedText(text, words) {
  let out = String(text ?? "");
  for (const word of words) {
    if (!word) continue;
    const escaped = word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(escaped, "gi"), "[typed text]");
  }
  return out;
}
