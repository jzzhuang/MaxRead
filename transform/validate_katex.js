#!/usr/bin/env node
/**
 * KaTeX validation script for Feishu document conversion.
 *
 * Reads JSON from stdin: an array of {id, latex} objects.
 * Outputs JSON to stdout: an array of {id, ok, error?} objects.
 *
 * Usage:
 *   echo '[{"id":0,"latex":"x^2"}]' | node validate_katex.js
 *   => [{"id":0,"ok":true}]
 *
 *   echo '[{"id":0,"latex":"\\frac{"}]' | node validate_katex.js
 *   => [{"id":0,"ok":false,"error":"Expected '}', got 'EOF' ..."}]
 */
const katex = require("katex");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  let items;
  try {
    items = JSON.parse(input);
  } catch {
    process.stderr.write("Invalid JSON input\n");
    process.exit(1);
  }

  if (!Array.isArray(items)) {
    process.stderr.write("Input must be a JSON array\n");
    process.exit(1);
  }

  const results = items.map(({ id, latex }) => {
    try {
      // Parse only — we don't need HTML output, just validation.
      // throwOnError: true makes KaTeX throw on invalid LaTeX.
      katex.renderToString(String(latex), {
        throwOnError: true,
        // Be lenient with unknown commands — Feishu's renderer may
        // support commands KaTeX doesn't.  We still catch structural
        // errors (unmatched braces, bad \frac args, etc.).
        strict: false,
        // Allow display-mode commands in inline context and vice versa.
        displayMode: false,
      });
      return { id, ok: true };
    } catch (e) {
      return { id, ok: false, error: e.message || String(e) };
    }
  });

  process.stdout.write(JSON.stringify(results) + "\n");
});
