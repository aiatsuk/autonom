import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";

const ROOT = resolve(import.meta.dirname, "..");
const BRIDGE = join(
  ROOT,
  "plugins/autonom/skills/android-emulator-browser/scripts/android-emulator-browser.mjs",
);

const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Wl8sAAAAASUVORK5CYII=";

async function waitForPreview(child, timeoutMs = 8000) {
  return await new Promise((resolvePromise, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error(`bridge did not start; output: ${output}`)), timeoutMs);
    child.stdout.on("data", (chunk) => {
      output += chunk.toString();
      const match = output.match(/Preview at (http:\/\/127\.0\.0\.1:\d+\/\?token=[^\s]+)/);
      if (match) {
        clearTimeout(timer);
        resolvePromise(match[1]);
      }
    });
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", (code) => {
      if (!output.includes("Preview at")) {
        clearTimeout(timer);
        reject(new Error(`bridge exited ${code}; output: ${output}`));
      }
    });
  });
}

test("bridge protects endpoints, streams frames, and forwards input", { timeout: 20000 }, async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "autonom-bridge-"));
  const fakeAdb = join(directory, "fake-adb.py");
  const adbLog = join(directory, "adb.log");
  await writeFile(
    fakeAdb,
    `#!/usr/bin/env python3
import base64
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_ADB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
if len(args) >= 2 and args[0] == "-s":
    args = args[2:]
if args == ["get-state"]:
    print("device", flush=True)
elif args == ["shell", "screenrecord", "--help"]:
    print("screenrecord test stub", flush=True)
elif args == ["shell", "wm", "size"]:
    print("Physical size: 1080x2400", flush=True)
elif args == ["exec-out", "screencap", "-p"]:
    sys.stdout.buffer.write(base64.b64decode("${PNG_BASE64}"))
    sys.stdout.buffer.flush()
elif len(args) >= 3 and args[0:2] == ["shell", "input"]:
    pass
else:
    print("unsupported fake adb command: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
`,
    "utf8",
  );
  await chmod(fakeAdb, 0o755);

  const child = spawn(
    process.execPath,
    [
      BRIDGE,
      "--adb", fakeAdb,
      "--serial", "emulator-5554",
      "--port", "0",
      "--transport", "screencap",
      "--fps", "1",
      "--token", "bridge-test-token",
    ],
    {
      cwd: ROOT,
      env: { ...process.env, FAKE_ADB_LOG: adbLog },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  t.after(() => {
    if (!child.killed) child.kill("SIGTERM");
  });

  const preview = await waitForPreview(child);
  const parsed = new URL(preview);
  const origin = parsed.origin;
  const token = parsed.searchParams.get("token");
  assert.equal(token, "bridge-test-token");

  const unauthorized = await fetch(`${origin}/status`);
  assert.equal(unauthorized.status, 401);

  const status = await fetch(`${origin}/status?token=${encodeURIComponent(token)}`);
  assert.equal(status.status, 200, stderr);
  const statusBody = await status.json();
  assert.equal(statusBody.serial, "emulator-5554");
  assert.equal(statusBody.transport, "screencap");
  assert.deepEqual(statusBody.display, { width: 1080, height: 2400 });

  const frame = await fetch(`${origin}/frame?token=${encodeURIComponent(token)}`);
  assert.equal(frame.status, 200);
  const frameBytes = new Uint8Array(await frame.arrayBuffer());
  assert.deepEqual([...frameBytes.slice(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);

  const tap = await fetch(`${origin}/tap?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ x: 120, y: 340 }),
  });
  assert.equal(tap.status, 200);
  assert.deepEqual(await tap.json(), { ok: true, x: 120, y: 340 });

  const controller = new AbortController();
  const stream = await fetch(`${origin}/stream.mjpeg?token=${encodeURIComponent(token)}`, {
    signal: controller.signal,
  });
  assert.equal(stream.status, 200);
  assert.match(stream.headers.get("content-type") ?? "", /multipart\/x-mixed-replace/);
  const reader = stream.body.getReader();
  const firstChunk = await reader.read();
  assert.equal(firstChunk.done, false);
  const chunkText = Buffer.from(firstChunk.value).toString("latin1");
  assert.match(chunkText, /autonom-frame|Content-Type: image\/png/);
  controller.abort();
  await reader.cancel().catch(() => {});

  await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
  const log = await readFile(adbLog, "utf8");
  assert.match(log, /shell input tap 120 340/);
});
