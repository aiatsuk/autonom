/** Owned screenshot payloads from an explicitly configured, local emulator gRPC.
 * No shared-memory channel, new listeners, token creation or network setup here.
 */
import { connect } from "node:http2";
import { readFile, mkdir, rename, rm, stat, readdir, open } from "node:fs/promises";
import { constants } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { spawn, execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
export const MAX_IMAGE_BYTES = 64 * 1024 * 1024;
const source = join(dirname(fileURLToPath(import.meta.url)), "host-video.swift");
const configPath = join(homedir(), ".config", "autonom", "host-video.json");

export function settings(document, serial) {
  if (document?.version !== 1 || !Array.isArray(document.devices)) return null;
  const matches = document.devices.filter(d => d?.serial === serial);
  if (matches.length !== 1) return null;
  const d = matches[0];
  if (document.devices.some(other => other !== d && (other?.grpcPort === d.grpcPort || other?.avd === d.avd))) return null;
  if (!/^emulator-\d+$/.test(d.serial) || Number(d.serial.slice(9)) < 5554 || Number(d.serial.slice(9)) > 5682 ||
      Number(d.serial.slice(9)) % 2 || typeof d.avd !== "string" || !/^[A-Za-z0-9._-]+$/.test(d.avd) ||
      !Number.isInteger(d.grpcPort) || d.grpcPort < 1024 || d.grpcPort > 65535 ||
      d.grpcPort === Number(d.serial.slice(9)) || d.grpcPort === Number(d.serial.slice(9))+1) return null;
  return { serial: d.serial, avd: d.avd, grpcPort: d.grpcPort, manageCutout: d.manageCutout === true };
}

/** Bounded protobuf reader: unknown fields skipped, malformed wire data rejected. */
export function fields(bytes) {
  let offset = 0;
  const values = new Map();
  function varint() {
    let n = 0n;
    for (let i = 0; i < 10; i++) {
      if (offset >= bytes.length) throw Error("truncated protobuf");
      const b = bytes[offset++];
      if (i === 9 && b > 1) throw Error("protobuf overflow");
      n |= BigInt(b & 127) << BigInt(i*7);
      if (!(b & 128)) return n;
    }
    throw Error("protobuf overflow");
  }
  while (offset < bytes.length) {
    if (values.size > 128) throw Error("too many protobuf fields");
    const tag = varint(), id = Number(tag >> 3n), wire = Number(tag & 7n);
    if (id <= 0 || id > 0x1fffffff) throw Error("invalid protobuf tag");
    if (wire === 0) values.set(id, varint());
    else {
      const size = wire === 1 ? 8 : wire === 5 ? 4 : wire === 2 ? Number(varint()) : -1;
      if (!Number.isSafeInteger(size) || size < 0 || size > bytes.length-offset) throw Error("invalid protobuf length");
      values.set(id, bytes.subarray(offset, offset+size)); offset += size;
    }
  }
  return values;
}
export function parseImage(bytes) {
  const f = fields(bytes), format = f.get(1), pixels = f.get(4);
  if (!Buffer.isBuffer(format)) throw Error("missing image format");
  const info = fields(format), width = Number(info.get(3) ?? 0n), height = Number(info.get(4) ?? 0n);
  if ((!pixels || !pixels.length) && (!width || !height)) return null; // display is asleep
  if (!Number.isInteger(width) || !Number.isInteger(height) || Number(info.get(1)) !== 1 || width < 2 || height < 2 || width > 8192 || height > 8192 ||
      width*height*4 > MAX_IMAGE_BYTES || !Buffer.isBuffer(pixels) || pixels.length !== width*height*4) throw Error("invalid RGBA image");
  const rotation = Buffer.isBuffer(info.get(2)) ? Number(fields(info.get(2)).get(1) ?? 0n) : 0;
  if (!Number.isInteger(rotation) || rotation < 0 || rotation > 3) throw Error("invalid image rotation");
  return {width, height, rotation, pixels, seq: Number(f.get(5) ?? 0n)};
}
export function requestImage(maxSize) {
  function v(n) { const b=[]; do { let p=n%128; n=Math.floor(n/128); b.push(p|(n?128:0)); } while(n); return b; }
  if (!Number.isInteger(maxSize) || maxSize < 0 || maxSize > 8192) throw Error("invalid capture size");
  const payload = Buffer.from([8,1,24,...v(maxSize),32,...v(maxSize)]);
  const header=Buffer.alloc(5); header.writeUInt32BE(payload.length,1);
  return Buffer.concat([header,payload]);
}
/** One allocation per framed message; never repeatedly concatenate a growing image. */
export class GrpcFrames {
  constructor(consume) { this.consume=consume; this.header=Buffer.alloc(5); this.used=0; this.body=null; this.bodyUsed=0; }
  push(chunk) {
    let at=0;
    while(at<chunk.length) {
      if (!this.body) {
        const n=Math.min(5-this.used,chunk.length-at);
        chunk.copy(this.header,this.used,at,at+n); at+=n; this.used+=n;
        if(this.used<5) continue;
        const size=this.header.readUInt32BE(1);
        if(this.header[0]!==0 || size===0 || size>MAX_IMAGE_BYTES+4096) throw Error("invalid gRPC frame");
        this.body=Buffer.allocUnsafe(size); this.bodyUsed=0;
      }
      const n=Math.min(this.body.length-this.bodyUsed,chunk.length-at);
      chunk.copy(this.body,this.bodyUsed,at,at+n);at+=n;this.bodyUsed+=n;
      if(this.bodyUsed===this.body.length) {
        const frame=this.body; this.body=null;this.used=0;this.consume(frame);
      }
    }
  }
  finish() { if(this.used || this.body) throw Error("truncated gRPC frame"); }
}
export function frameHeader(image, logical, maxSize) {
  let w=logical?.width ?? image.width, h=logical?.height ?? image.height;
  const rotation=logical?.rotation ?? image.rotation;
  if(rotation%2 && logical) [w,h]=[h,w];
  const scale=Math.min(1,(maxSize||8192)/Math.max(w,h));
  w=Math.max(2,Math.floor(w*scale/2)*2); h=Math.max(2,Math.floor(h*scale/2)*2);
  const header=Buffer.alloc(32);header.writeUInt32BE(0x41463031);
  [image.pixels.length,image.width,image.height,image.width*4,1+(((rotation-image.rotation+4)%4)<<8),w,h].forEach((v,i)=>header.writeUInt32BE(v,4+i*4));
  return header;
}
export function keyHeader() { const h=Buffer.alloc(32);h.writeUInt32BE(0x41463031);return h; }

let compiling;
async function helperBinary() {
  if(compiling) return compiling;
  compiling=(async()=>{
    const data=await readFile(source);
    const hash=createHash("sha256").update(data).update(process.arch).digest("hex");
    const directory=join(homedir(),".cache","autonom","host-video",hash);
    const binary=join(directory,"encoder");
    if(await stat(binary).then(s=>s.isFile(),()=>false))return binary;
    await mkdir(directory,{recursive:true,mode:0o700});
    const temp=binary+"."+randomUUID();
    try {
      await exec("/usr/bin/xcrun",["swiftc","-O","-warnings-as-errors",source,"-o",temp],{timeout:60000,maxBuffer:64000});
      await rename(temp,binary);
    } finally { await rm(temp,{force:true}); }
    return binary;
  })().catch(error=>{compiling=null;throw error});
  return compiling;
}

/** Only the current emulator's private discovery record supplies its ephemeral token.
 * Never log the record: it can contain credentials and the complete launch command.
 */
export function discoveryRecord(text, config) {
  const values = new Map();
  for (const line of text.split(/\r?\n/)) {
    const equals = line.indexOf("=");
    if (equals > 0) values.set(line.slice(0,equals).trim(),line.slice(equals+1).trim());
  }
  if (values.get("port.serial") !== config.serial.slice(9) ||
      values.get("grpc.port") !== String(config.grpcPort) ||
      values.get("avd.id") !== config.avd) return null;
  const token=values.get("grpc.token");
  if (!/^[A-Za-z0-9+/_=-]{16,512}$/.test(token ?? "")) return null;
  // This path intentionally never disables TLS validation or consumes private keys.
  if (values.has("grpc.server_cert") || values.has("grpc.ca_root")) return null;
  return token;
}
async function discoveryToken(config) {
  const directory=join(homedir(),"Library","Caches","TemporaryItems","avd","running");
  const entries=await readdir(directory);
  for (const name of entries) {
    const match=/^pid_([1-9][0-9]*)(?:_info)?\.ini$/.exec(name);
    if(!match)continue;
    let handle;
    try {
      process.kill(Number(match[1]),0); // stale advertisements never authorize a connection
      handle=await open(join(directory,name),constants.O_RDONLY|constants.O_NOFOLLOW);
      const info=await handle.stat();
      if(!info.isFile() || info.uid!==process.getuid() || (info.mode & 0o022) ||
         info.size>64*1024)continue;
      const token=discoveryRecord(await handle.readFile("utf8"),config);
      if(token)return token;
    } catch { /* vanished/stale discovery entry */ }
    finally { await handle?.close(); }
  }
  throw Error("authenticated emulator discovery unavailable");
}

/** Explicit configured emulator only. Missing support leaves normal scrcpy intact. */
export async function prepareHostVideo({serial, adbPath, maxSize, maxFps, bitRate, geometry, orientation, decorations,
  platform=process.platform, read=readFile, build=helperBinary, run=exec, authenticate=discoveryToken}) {
  if(platform!=="darwin") return null;
  let config;
  try { config=settings(JSON.parse(await read(configPath,"utf8")),serial); } catch { return null; }
  if(!config) return null;
  // A serial can be reused by another AVD: verify identity before contacting gRPC.
  const avd=await run(adbPath,["-s",serial,"emu","avd","name"],{timeout:3000});
  if(String(avd.stdout).split(/\r?\n/)[0].trim()!==config.avd) throw Error("host video AVD mismatch");
  const token=await authenticate(config);
  const binary=await build();
  if(orientation)await orientation();
  return () => new HostVideo({port:config.grpcPort,token,binary,maxSize,maxFps,bitRate,geometry,
    decorate: () => decorations ? decorations.acquire(() => prepareCutout({config,adbPath,run}))
      : prepareCutout({config,adbPath,run})});
}

/** Keep decorations stable while an attached device is temporarily hidden or reprofiled.
 * Restore at device detach/Canvas shutdown, or immediately on capture failure.
 */
export class CaptureDecorations {
  async acquire(create) {
    this.ready ??= create().catch(error=>{this.ready=null;throw error});
    await this.ready;
    return async failed=>{if(failed)await this.restore()};
  }
  async refresh() {
    if(this.ready)await (await this.ready).refresh?.();
  }
  async restore() {
    const ready=this.ready;this.ready=null;
    if(ready)await (await ready)();
  }
}

/** Only an explicitly approved emulator may temporarily suppress its physical cutout drawing.
 * This single resource does not alter app insets or privacy indicators. Restore before fallback.
 */
export async function prepareCutout({config,adbPath,run=exec}) {
  if(!config.manageCutout)return async()=>{};
  const overlay="com.android.shell:AutonomHostCapture";
  const adb=args=>run(adbPath,["-s",config.serial,"shell",...args],{timeout:5000,maxBuffer:64000});
  const listed=await adb(["cmd","overlay","list","android"]);
  if(String(listed.stdout).includes("[x] "+overlay))
    await adb(["cmd","overlay","disable",overlay]); // recover our interrupted previous session
  const before=await adb(["cmd","overlay","lookup","android","android:bool/config_fillMainBuiltInDisplayCutout"]);
  if(String(before.stdout).trim()==="false")return async()=>{};
  if(String(before.stdout).trim()!=="true")throw Error("unknown display decoration state");
  const restore=async()=>{await adb(["cmd","overlay","disable",overlay]);};
  restore.refresh=async()=>{
    const current=await adb(["cmd","overlay","lookup","android","android:bool/config_fillMainBuiltInDisplayCutout"]);
    if(String(current.stdout).trim()==="false")return;
    // Fabricated overlays do not support set-priority on this Android version.
    // Re-enable only when a model change has actually superseded our resource.
    await adb(["cmd","overlay","disable",overlay]);
    await adb(["cmd","overlay","enable",overlay]);
  };
  await adb(["su","0","cmd","overlay","fabricate","--target","android","--name","AutonomHostCapture",
    "android:bool/config_fillMainBuiltInDisplayCutout","0x12","0x0"]);
  try {
    await adb(["cmd","overlay","enable",overlay]);
    await restore.refresh();
    const after=await adb(["cmd","overlay","lookup","android","android:bool/config_fillMainBuiltInDisplayCutout"]);
    if(String(after.stdout).trim()!=="false")throw Error("display decoration override unavailable");
    return restore;
  } catch(error) { await restore();throw error; }
}

/** Permit a two-frame delivery burst without exceeding the long-term FPS cap. */
export class FramePacer {
  constructor(fps) { this.interval=1000/fps; this.next=-Infinity; }
  accept(now) {
    if(now < this.next-this.interval) return false;
    this.next=Math.max(now,this.next)+this.interval;
    return true;
  }
}

export class HostVideo {
  constructor(options) {
    const {binary,maxFps,bitRate,decorate}=options;
    this.closed=false;this.busy=false;this.keyWanted=false;this.pacer=new FramePacer(maxFps);
    this.child=spawn(binary,[],{stdio:["pipe","pipe","pipe"],env:{...process.env,
      AUTONOM_HOST_FPS:String(maxFps),AUTONOM_HOST_BIT_RATE:String(bitRate)}});
    this.stdout=this.child.stdout;
    this.stats={received:0,skipped:0,busySkipped:0,rateSkipped:0,sourceGaps:0};
    this.exit=new Promise(resolve=>this.child.once("close",resolve));
    this.child.once("error",()=>this.abort());
    this.child.stdin.on("error",()=>this.abort());
    this.child.stderr.resume();
    this.child.once("close",()=>{if(!this.closed)this.failed=true;this.stop()});
    this.setup=(async()=>{
      this.restore=await (decorate?.() ?? Promise.resolve(async()=>{}));
      if(!this.closed)this.capture(options);
    })().catch(()=>{queueMicrotask(()=>this.abort())});
  }
  capture({port,token,maxSize,maxFps,geometry}) {
    this.client=connect("http://127.0.0.1:"+port,{settings:{initialWindowSize:1024*1024}});
    this.client.on("error",()=>this.abort());
    this.client.once("connect",()=>this.client.setLocalWindowSize(8*1024*1024));
    this.request=this.client.request({":method":"POST",":path":"/android.emulation.control.EmulatorController/streamScreenshot",
      "content-type":"application/grpc",te:"trailers",authorization:"Bearer "+token});
    this.request.on("error",()=>this.abort());
    this.request.on("response",h=>{if(h[":status"]!==200)this.abort()});
    this.request.on("trailers",h=>{if(h["grpc-status"]!=="0")this.abort()});
    this.request.on("end",()=>this.abort());
    const parser=new GrpcFrames(bytes=>{
      const image=parseImage(bytes);
      if(!image)return;
      clearTimeout(this.startTimer);
      this.stats.received++;
      if(this.sequence!==undefined)this.stats.sourceGaps+=Math.max(0,image.seq-this.sequence-1);
      this.sequence=image.seq;
      const now=performance.now();
      if(this.busy){this.stats.skipped++;this.stats.busySkipped++;return}
      if(!this.pacer.accept(now)){this.stats.skipped++;this.stats.rateSkipped++;return}
      this.stats.sourceWidth=image.width;this.stats.sourceHeight=image.height;
      this.busy=true;
      this.child.stdin.cork();
      this.child.stdin.write(frameHeader(image,geometry?.(),maxSize));
      this.child.stdin.write(image.pixels,()=>{
        this.busy=false;
        if(this.keyWanted){this.keyWanted=false;this.requestKeyFrame()}
      });
      this.child.stdin.uncork();
    });
    this.request.on("data",chunk=>{try{parser.push(chunk)}catch{this.abort()}});
    this.startTimer=setTimeout(()=>this.abort(),10000);
    this.request.end(requestImage(maxSize));
  }
  requestKeyFrame() {
    if(this.closed)return;
    if(this.busy){this.keyWanted=true;return}
    this.child.stdin.write(keyHeader());
  }
  abort() { if(this.closed)return;this.failed=true;this.stop(); }
  stop() {
    if(this.stopping)return this.stopping;
    this.closed=true;clearTimeout(this.startTimer);
    this.request?.destroy();this.client?.destroy();
    this.child.stdin.destroy();this.child.kill("SIGTERM");
    const timer=setTimeout(()=>this.child.kill("SIGKILL"),1000);timer.unref();
    this.exit.finally(()=>clearTimeout(timer));
    this.stopping=Promise.all([this.exit,this.setup]).then(()=>this.restore?.(this.failed));
    // Preserve rejection for the session teardown, but avoid an unhandled rejection
    // when stop originated from the helper's exit event.
    this.stopping.catch(()=>{console.error("Host capture decoration restore failed");});
    return this.stopping;
  }
}
