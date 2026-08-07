#!/usr/bin/env node
import { access, constants } from "node:fs/promises";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { delimiter, join } from "node:path";
import { env, exit, platform } from "node:process";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";

import {
  MAX_BODY_BYTES,
  encodeAdbText,
  extractJpegFrames,
  generateToken,
  isSafeKeyCode,
  normalizeCoordinate,
  parseArgs,
  parseWmSize,
} from "./browser-lib.mjs";

const execFileAsync = promisify(execFile);
const BOUNDARY = "autonom-frame";

main().catch((error) => {
  console.error(`android-emulator-browser: ${error.message}`);
  exit(1);
});

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }

  const adbPath = options.adb ?? await findAdb();
  const serial = options.serial ?? await inferSingleDevice(adbPath);
  await assertDevice(adbPath, serial);

  let ffmpegPath = options.ffmpeg;
  if (!ffmpegPath) ffmpegPath = await findExecutable("ffmpeg").catch(() => null);
  const screenrecordSupported = await supportsScreenrecord(adbPath, serial);
  if (options.transport === "screenrecord" && (!ffmpegPath || !screenrecordSupported)) {
    throw new Error("screenrecord transport requires device H.264 output support and ffmpeg on PATH");
  }

  const token = options.noAuth ? "" : (options.token ?? generateToken());
  const state = {
    startedAt: Date.now(),
    framesSent: 0,
    lastFrameAt: null,
    streamClients: 0,
    acceleratedFailed: false,
    lastError: null,
  };
  let inputQueue = Promise.resolve();

  const context = {
    options,
    adbPath,
    serial,
    ffmpegPath,
    screenrecordSupported,
    token,
    state,
    enqueueInput(task) {
      inputQueue = inputQueue.catch(() => {}).then(task);
      return inputQueue;
    },
  };

  const server = createServer((request, response) => {
    handleRequest(context, request, response).catch((error) => {
      state.lastError = error.message;
      if (!response.headersSent) {
        sendJson(response, error.statusCode ?? 500, { error: error.message });
      } else {
        response.destroy(error);
      }
    });
  });

  server.listen(options.port, "127.0.0.1", () => {
    const address = server.address();
    const port = typeof address === "object" && address ? address.port : options.port;
    const query = token ? `?token=${encodeURIComponent(token)}` : "";
    const url = `http://127.0.0.1:${port}/${query}`;
    console.log(`android-emulator-browser ready for ${serial}`);
    console.log(`Transport preference: ${options.transport}`);
    console.log(`Preview at ${url}`);
    console.log(`Open this exact URL in the visible Codex side-panel browser: ${url}`);
    if (!token) console.warn("WARNING: authentication is disabled");
  });

  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    server.close(() => exit(0));
    server.closeAllConnections?.();
    setTimeout(() => exit(0), 1000).unref();
  };
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(signal, shutdown);
  }
}

function printHelp() {
  console.log(`android-emulator-browser

Token-protected Android device preview and input bridge for the Codex side-panel browser.

Usage:
  android-emulator-browser --serial <adb-serial> [options]

Options:
  --serial, -s SERIAL          Explicit adb serial.
  --adb PATH                  adb executable path.
  --ffmpeg PATH               ffmpeg executable path.
  --port, -p PORT             Localhost port (default: 3277; 0 chooses a free port).
  --transport MODE            auto, screenrecord, or screencap (default: auto).
  --fps FPS                   Requested accelerated FPS (default: 15).
  --max-size PX               Maximum accelerated video width (default: 1280).
  --bit-rate BPS              H.264 bitrate (default: 8000000).
  --token TOKEN               Use a supplied access token.
  --no-auth                   Disable token protection (isolated local use only).
`);
}

async function handleRequest(context, request, response) {
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  if (!isAuthorized(context, request, url)) {
    sendJson(response, 401, { error: "Unauthorized" });
    return;
  }
  if (request.method === "GET" && url.pathname === "/") {
    sendHtml(response, renderPage(context));
  } else if (request.method === "GET" && url.pathname === "/status") {
    await sendStatus(context, response);
  } else if (request.method === "GET" && url.pathname === "/frame") {
    await sendFrame(context, response);
  } else if (request.method === "GET" && url.pathname === "/stream.mjpeg") {
    await sendStream(context, request, response);
  } else if (request.method === "POST" && url.pathname === "/tap") {
    await tap(context, response, await readJsonBody(request));
  } else if (request.method === "POST" && url.pathname === "/swipe") {
    await swipe(context, response, await readJsonBody(request));
  } else if (request.method === "POST" && url.pathname === "/key") {
    await key(context, response, await readJsonBody(request));
  } else if (request.method === "POST" && url.pathname === "/text") {
    await text(context, response, await readJsonBody(request));
  } else {
    sendJson(response, 404, { error: "Not found" });
  }
}

function isAuthorized(context, request, url) {
  if (!context.token) return true;
  const queryToken = url.searchParams.get("token");
  const authorization = request.headers.authorization ?? "";
  const bearer = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  return queryToken === context.token || bearer === context.token;
}

function chooseTransport(context) {
  const { options, ffmpegPath, screenrecordSupported, state } = context;
  if (options.transport === "screencap") return "screencap";
  if (options.transport === "screenrecord") return "screenrecord";
  if (ffmpegPath && screenrecordSupported && !state.acceleratedFailed) return "screenrecord";
  return "screencap";
}

async function sendStatus(context, response) {
  const { stdout } = await runAdb(context, ["shell", "wm", "size"], { timeout: 3000 });
  sendJson(response, 200, {
    serial: context.serial,
    display: parseWmSize(stdout),
    transport: chooseTransport(context),
    requested_transport: context.options.transport,
    ffmpeg: Boolean(context.ffmpegPath),
    screenrecord_h264: context.screenrecordSupported,
    frames_sent: context.state.framesSent,
    last_frame_at: context.state.lastFrameAt,
    stream_clients: context.state.streamClients,
    uptime_seconds: Math.round((Date.now() - context.state.startedAt) / 1000),
    last_error: context.state.lastError,
  });
}

async function sendFrame(context, response) {
  const { stdout } = await runAdb(context, ["exec-out", "screencap", "-p"], {
    timeout: 8000,
    encoding: "buffer",
  });
  if (!isPng(stdout)) throw httpError(502, "adb screencap did not return PNG data");
  response.writeHead(200, {
    "Content-Type": "image/png",
    "Cache-Control": "no-store",
    "Content-Length": stdout.length,
  });
  response.end(stdout);
}

async function sendStream(context, request, response) {
  response.writeHead(200, {
    "Content-Type": `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
    "Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
  });
  context.state.streamClients += 1;
  const transport = chooseTransport(context);
  try {
    if (transport === "screenrecord") {
      await streamScreenrecord(context, request, response);
    } else {
      await streamScreencap(context, request, response);
    }
  } finally {
    context.state.streamClients = Math.max(0, context.state.streamClients - 1);
    if (!response.writableEnded && !response.destroyed) response.end();
  }
}

function writeJpeg(context, response, frame) {
  if (response.destroyed || response.writableEnded) return false;
  response.write(`--${BOUNDARY}\r\n`);
  response.write("Content-Type: image/jpeg\r\n");
  response.write(`Content-Length: ${frame.length}\r\n\r\n`);
  response.write(frame);
  response.write("\r\n");
  context.state.framesSent += 1;
  context.state.lastFrameAt = new Date().toISOString();
  return true;
}

async function streamScreencap(context, request, response) {
  let closed = false;
  request.on("close", () => { closed = true; });
  const fps = Math.min(10, context.options.fps);
  const interval = Math.round(1000 / fps);
  while (!closed && !response.destroyed) {
    const started = Date.now();
    try {
      const { stdout } = await runAdb(context, ["exec-out", "screencap", "-p"], {
        timeout: 8000,
        encoding: "buffer",
      });
      if (!isPng(stdout)) throw new Error("screencap returned invalid PNG");
      // Browsers accept PNG frames in a multipart image stream despite the endpoint name.
      response.write(`--${BOUNDARY}\r\nContent-Type: image/png\r\nContent-Length: ${stdout.length}\r\n\r\n`);
      response.write(stdout);
      response.write("\r\n");
      context.state.framesSent += 1;
      context.state.lastFrameAt = new Date().toISOString();
    } catch (error) {
      context.state.lastError = error.message;
      await sleep(500);
    }
    const remaining = interval - (Date.now() - started);
    if (remaining > 0) await sleep(remaining);
  }
}

async function streamScreenrecord(context, request, response) {
  const { adbPath, serial, ffmpegPath, options, state } = context;
  const adb = spawn(adbPath, [
    "-s", serial,
    "exec-out", "screenrecord",
    "--output-format=h264",
    "--bit-rate", String(options.bitRate),
    "-",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  const filter = `fps=${options.fps},scale=min(${options.maxSize}\\,iw):-2`;
  const ffmpeg = spawn(ffmpegPath, [
    "-hide_banner", "-loglevel", "error",
    "-f", "h264", "-i", "pipe:0",
    "-an", "-vf", filter,
    "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1",
  ], { stdio: ["pipe", "pipe", "pipe"] });

  adb.stdout.pipe(ffmpeg.stdin);
  let carry = Buffer.alloc(0);
  let receivedFrame = false;
  let stderr = "";
  adb.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
  ffmpeg.stderr.on("data", (chunk) => { stderr += chunk.toString(); });

  const cleanup = () => {
    adb.kill("SIGTERM");
    ffmpeg.kill("SIGTERM");
  };
  request.once("close", cleanup);
  response.once("close", cleanup);

  await new Promise((resolve, reject) => {
    ffmpeg.stdout.on("data", (chunk) => {
      const parsed = extractJpegFrames(chunk, carry);
      carry = parsed.carry;
      for (const frame of parsed.frames) {
        receivedFrame = true;
        if (!writeJpeg(context, response, frame)) break;
      }
    });
    ffmpeg.once("error", reject);
    adb.once("error", reject);
    ffmpeg.once("close", (code) => {
      if (!receivedFrame) {
        state.acceleratedFailed = true;
        state.lastError = `accelerated stream failed${code === null ? "" : ` (exit ${code})`}: ${stderr.trim()}`;
      }
      resolve();
    });
  });
}

async function tap(context, response, body) {
  const x = normalizeCoordinate(body.x, "x");
  const y = normalizeCoordinate(body.y, "y");
  await context.enqueueInput(() => runAdb(context, ["shell", "input", "tap", String(x), String(y)]));
  sendJson(response, 200, { ok: true, x, y });
}

async function swipe(context, response, body) {
  const x1 = normalizeCoordinate(body.x1, "x1");
  const y1 = normalizeCoordinate(body.y1, "y1");
  const x2 = normalizeCoordinate(body.x2, "x2");
  const y2 = normalizeCoordinate(body.y2, "y2");
  const duration = Math.min(5000, Math.max(1, normalizeCoordinate(body.duration ?? 250, "duration")));
  await context.enqueueInput(() => runAdb(context, [
    "shell", "input", "swipe", String(x1), String(y1), String(x2), String(y2), String(duration),
  ]));
  sendJson(response, 200, { ok: true, x1, y1, x2, y2, duration });
}

async function key(context, response, body) {
  const value = String(body.key ?? "");
  if (!isSafeKeyCode(value)) throw httpError(400, "Unsupported key code");
  await context.enqueueInput(() => runAdb(context, ["shell", "input", "keyevent", value]));
  sendJson(response, 200, { ok: true, key: value });
}

async function text(context, response, body) {
  let encoded;
  try {
    encoded = encodeAdbText(String(body.text ?? ""));
  } catch (error) {
    throw httpError(400, error.message);
  }
  await context.enqueueInput(() => runAdb(context, ["shell", "input", "text", encoded]));
  sendJson(response, 200, { ok: true });
}

async function readJsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw httpError(413, "Request body is too large");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    throw httpError(400, "Invalid JSON body");
  }
}

async function runAdb(context, args, options = {}) {
  const encoding = options.encoding === "buffer" ? "buffer" : "utf8";
  const result = await execFileAsync(context.adbPath, ["-s", context.serial, ...args], {
    timeout: options.timeout ?? 10_000,
    maxBuffer: 32 * 1024 * 1024,
    encoding: encoding === "buffer" ? null : "utf8",
  });
  return { stdout: result.stdout, stderr: result.stderr };
}

async function supportsScreenrecord(adbPath, serial) {
  try {
    const { stdout, stderr } = await execFileAsync(adbPath, ["-s", serial, "shell", "screenrecord", "--help"], {
      timeout: 4000,
      encoding: "utf8",
    });
    return `${stdout}\n${stderr}`.includes("--output-format");
  } catch (error) {
    const output = `${error.stdout ?? ""}\n${error.stderr ?? ""}`;
    return output.includes("--output-format");
  }
}

async function assertDevice(adbPath, serial) {
  const { stdout } = await execFileAsync(adbPath, ["-s", serial, "get-state"], { timeout: 5000, encoding: "utf8" });
  if (stdout.trim() !== "device") throw new Error(`adb target is not ready: ${serial}`);
}

async function inferSingleDevice(adbPath) {
  const { stdout } = await execFileAsync(adbPath, ["devices"], { timeout: 5000, encoding: "utf8" });
  const devices = stdout.split(/\r?\n/).slice(1)
    .map((line) => line.trim().split(/\s+/))
    .filter((parts) => parts.length >= 2 && parts[1] === "device")
    .map((parts) => parts[0]);
  if (devices.length === 1) return devices[0];
  if (!devices.length) throw new Error("No authorized adb device is connected. Pass --serial after starting one.");
  throw new Error(`Multiple adb devices are connected (${devices.join(", ")}). Pass --serial.`);
}

async function findAdb() {
  const candidates = [];
  const found = await findExecutable("adb").catch(() => null);
  if (found) candidates.push(found);
  for (const root of [env.ANDROID_SDK_ROOT, env.ANDROID_HOME, join(homedir(), "Library/Android/sdk")]) {
    if (root) candidates.push(join(root, "platform-tools", platform === "win32" ? "adb.exe" : "adb"));
  }
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {}
  }
  throw new Error("adb not found on PATH, ANDROID_SDK_ROOT, ANDROID_HOME, or ~/Library/Android/sdk");
}

async function findExecutable(name) {
  const extensions = platform === "win32" ? [".exe", ".cmd", ".bat", ""] : [""];
  for (const directory of (env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    for (const extension of extensions) {
      const candidate = join(directory, name + extension);
      try {
        await access(candidate, constants.X_OK);
        return candidate;
      } catch {}
    }
  }
  throw new Error(`${name} not found on PATH`);
}

function renderPage(context) {
  const token = JSON.stringify(context.token);
  const serial = escapeHtml(context.serial);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonom</title>
<style>
:root{color-scheme:dark;font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;background:#111418;color:#f5f7f7}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;grid-template-columns:minmax(280px,1fr) 290px;gap:20px;padding:20px}
main{display:flex;align-items:center;justify-content:center;min-width:0}.device{width:min(100%,520px);aspect-ratio:9/19.5;background:#030506;border:9px solid #030506;border-radius:30px;overflow:hidden;box-shadow:0 24px 70px #0009;display:flex;align-items:center;justify-content:center;touch-action:none}
img{width:100%;height:100%;object-fit:contain;background:#000;user-select:none;-webkit-user-drag:none;touch-action:none;cursor:crosshair}
aside{display:flex;flex-direction:column;gap:12px;min-width:0}h1{font-size:20px;margin:0}.meta,.status{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#b8c3c0;white-space:pre-wrap;overflow-wrap:anywhere}
.controls{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}button,input{min-height:38px;border-radius:8px;border:1px solid #3b4643;background:#202724;color:#f5f7f7;padding:8px;font:inherit}button{cursor:pointer}button:hover{background:#2a3531}input{grid-column:1/-1}.wide{grid-column:1/-1}.accent{border-color:#00C2A8}
@media(max-width:780px){body{grid-template-columns:1fr;padding:12px}.device{width:min(100%,420px)}}
</style>
</head>
<body>
<main><div class="device"><img id="screen" alt="Android device screen"></div></main>
<aside>
  <div><h1>Autonom</h1><div class="meta">adb: ${serial}</div></div>
  <div class="controls">
    <button data-key="KEYCODE_BACK">Back</button><button data-key="KEYCODE_HOME">Home</button><button data-key="KEYCODE_APP_SWITCH">Apps</button>
    <button data-key="KEYCODE_DPAD_UP">↑</button><button data-key="KEYCODE_ENTER">Enter</button><button data-key="KEYCODE_DPAD_DOWN">↓</button>
    <button data-key="KEYCODE_DPAD_LEFT">←</button><button data-key="KEYCODE_DEL">Delete</button><button data-key="KEYCODE_DPAD_RIGHT">→</button>
    <button id="wake">Wake</button><button id="refresh" class="wide">Reconnect stream</button>
    <input id="text" autocomplete="off" placeholder="Safe ASCII text">
    <button id="sendText" class="wide accent">Type text</button>
  </div>
  <div class="status" id="status">Connecting…</div>
</aside>
<script>
const token=${token};
function url(path, cacheBust=false){
  const params=new URLSearchParams();
  if(token)params.set("token",token);
  if(cacheBust)params.set("ts",String(Date.now()));
  const query=params.toString();
  return path+(query?"?"+query:"");
}
const screen=document.getElementById("screen"),statusEl=document.getElementById("status");
let pointer=null,reconnectTimer=null;
function setStatus(value){statusEl.textContent=value}
function restart(){clearTimeout(reconnectTimer);screen.src=url("/stream.mjpeg",true)}
async function post(path,body){const response=await fetch(url(path),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});const payload=await response.json().catch(()=>({}));if(!response.ok)throw new Error(payload.error||response.statusText);return payload}
function point(event){const rect=screen.getBoundingClientRect(),nw=screen.naturalWidth,nh=screen.naturalHeight;if(!nw||!nh)return null;const ratio=Math.min(rect.width/nw,rect.height/nh),rw=nw*ratio,rh=nh*ratio,xoff=(rect.width-rw)/2,yoff=(rect.height-rh)/2,x=Math.round((event.clientX-rect.left-xoff)/ratio),y=Math.round((event.clientY-rect.top-yoff)/ratio);if(x<0||y<0||x>nw||y>nh)return null;return{x,y}}
screen.addEventListener("load",()=>setStatus("Video connected"));screen.addEventListener("error",()=>{setStatus("Stream reconnecting…");reconnectTimer=setTimeout(restart,600)});
screen.addEventListener("pointerdown",event=>{const p=point(event);if(!p)return;screen.setPointerCapture(event.pointerId);pointer={...p,time:Date.now(),id:event.pointerId}});
screen.addEventListener("pointerup",async event=>{if(!pointer)return;const end=point(event)||pointer,start=pointer;pointer=null;const duration=Math.max(1,Date.now()-start.time),distance=Math.hypot(end.x-start.x,end.y-start.y);try{if(distance<12&&duration<500)await post("/tap",{x:end.x,y:end.y});else await post("/swipe",{x1:start.x,y1:start.y,x2:end.x,y2:end.y,duration:Math.min(5000,duration)});}catch(error){setStatus(error.message)}});
screen.addEventListener("pointercancel",()=>{pointer=null});
screen.addEventListener("wheel",async event=>{event.preventDefault();if(!screen.naturalWidth)return;const x=Math.round(screen.naturalWidth/2),y1=Math.round(screen.naturalHeight*.55),y2=Math.round(screen.naturalHeight*(event.deltaY>0?.25:.78));try{await post("/swipe",{x1:x,y1,x2:x,y2,duration:220})}catch(error){setStatus(error.message)}},{passive:false});
for(const button of document.querySelectorAll("[data-key]")){button.addEventListener("click",()=>post("/key",{key:button.dataset.key}).catch(error=>setStatus(error.message)))}
document.getElementById("wake").onclick=()=>post("/key",{key:"KEYCODE_WAKEUP"}).catch(error=>setStatus(error.message));
document.getElementById("refresh").onclick=restart;
async function sendText(){const input=document.getElementById("text");try{await post("/text",{text:input.value});input.value=""}catch(error){setStatus(error.message)}}
document.getElementById("sendText").onclick=sendText;document.getElementById("text").addEventListener("keydown",event=>{if(event.key==="Enter")sendText()});
async function poll(){try{const response=await fetch(url("/status")),data=await response.json();if(!response.ok)throw new Error(data.error||response.statusText);const display=data.display?data.display.width+"x"+data.display.height:"unknown";const lastError=data.last_error?"\nlast error: "+data.last_error:"";setStatus("transport: "+data.transport+"\nframes: "+data.frames_sent+"\nclients: "+data.stream_clients+"\ndisplay: "+display+lastError)}catch(error){setStatus(error.message)}finally{setTimeout(poll,1200)}}
restart();poll();
</script>
</body>
</html>`;
}

function sendJson(response, status, value) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": body.length,
  });
  response.end(body);
}

function sendHtml(response, html) {
  const body = Buffer.from(html);
  response.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Length": body.length,
  });
  response.end(body);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function isPng(buffer) {
  return Buffer.isBuffer(buffer) && buffer.length > 8 && buffer.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]));
}

function httpError(statusCode, message) {
  const error = new Error(message);
  error.statusCode = statusCode;
  return error;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
