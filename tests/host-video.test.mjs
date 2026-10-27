import test from "node:test";
import assert from "node:assert/strict";
import {fields,parseImage,GrpcFrames,requestImage,frameHeader,keyHeader,settings,prepareHostVideo,discoveryRecord,FramePacer,prepareCutout,HostVideo,CaptureDecorations} from "../plugins/autonom/skills/android-emulator-browser/scripts/host-video.mjs";
const bytes=(id,payload)=>Buffer.concat([Buffer.from([id*8+2,payload.length]),payload]);
const image=Buffer.concat([bytes(1,Buffer.from([8,1,24,2,32,2])),bytes(4,Buffer.alloc(16,42))]);
test("owned image validates RGBA length and geometry",()=>{
 assert.equal(parseImage(image).width,2);
 assert.equal(parseImage(image).pixels.length,16);
 assert.throws(()=>parseImage(image.subarray(0,-1)));
 assert.throws(()=>parseImage(Buffer.concat([bytes(1,Buffer.from([8,1,24,3,32,2])),bytes(4,Buffer.alloc(16))])));
});
test("protobuf rejects overflow, truncation and illegal wire types",()=>{
 for(const b of [[0],[0x0a,0xff],[8,0x80],[0x0f],Array(11).fill(0xff)])assert.throws(()=>fields(Buffer.from(b)));
});
test("gRPC framing handles arbitrary fragmentation and multiple messages",()=>{
 const h=Buffer.alloc(5);h.writeUInt32BE(image.length,1);
 const data=Buffer.concat([h,image,h,image]),seen=[];
 const parser=new GrpcFrames(b=>seen.push(parseImage(b)));
 for(const b of data)parser.push(Buffer.from([b]));
 parser.finish();assert.equal(seen.length,2);
 const partial=new GrpcFrames(()=>{});partial.push(h);assert.throws(()=>partial.finish());
});
test("gRPC limits allocation before copying untrusted payload",()=>{
 for(const h of [Buffer.from([1,0,0,0,1]),Buffer.from([0,0xff,0xff,0xff,0xff]),Buffer.alloc(5)])
  assert.throws(()=>new GrpcFrames(()=>{}).push(h));
});
test("capture request uses owned RGBA channel, no MMAP",()=>{
 const b=requestImage(1280),f=fields(b.subarray(5));
 assert.equal(f.get(1),1n);assert.equal(f.get(3),1280n);assert.equal(f.get(4),1280n);assert.equal(f.has(6),false);
});
test("native header follows logical size, rotation, and cap",()=>{
 const img={width:720,height:1280,rotation:0,pixels:Buffer.alloc(720*1280*4)};
 let h=frameHeader(img,{width:1080,height:2400},1280);
 assert.equal(h.readUInt32BE(20),1); // Installed emulator sends top-down RGBA.
 assert.equal(h.readUInt32BE(24),576);assert.equal(h.readUInt32BE(28),1280);
 h=frameHeader({...img,rotation:1},{width:1080,height:2400},1280);
 assert.equal(h.readUInt32BE(24),1280);assert.equal(h.readUInt32BE(28),576);
 h=frameHeader(img,{width:720,height:1280,rotation:1},1280);
 assert.equal(h.readUInt32BE(20),257);assert.equal(h.readUInt32BE(24),1280);assert.equal(h.readUInt32BE(28),720);
 assert.equal(keyHeader().length,32);
});
test("host capture is scoped to one explicitly configured emulator",()=>{
 const d={version:1,devices:[{serial:"emulator-5556",avd:"Example",grpcPort:8556}]};
 assert.ok(settings(d,"emulator-5556"));assert.equal(settings(d,"emulator-5554"),null);
 assert.equal(settings({...d,devices:[...d.devices,...d.devices]},"emulator-5556"),null);
 assert.equal(settings({version:1,devices:[{...d.devices[0],grpcPort:5557}]},"emulator-5556"),null);
});
test("normal launch stays on Android without configuration or on non-Mac",async()=>{
 let reads=0;
 assert.equal(await prepareHostVideo({serial:"emulator-5556",platform:"linux",read:()=>reads++}),null);
 assert.equal(reads,0);
 assert.equal(await prepareHostVideo({serial:"emulator-5556",platform:"darwin",read:async()=>{throw Error("ENOENT")}}),null);
});
test("reused serial cannot use another AVD's host endpoint",async()=>{
 let built=false;
 await assert.rejects(prepareHostVideo({serial:"emulator-5556",platform:"darwin",adbPath:"fake",
 read:async()=>JSON.stringify({version:1,devices:[{serial:"emulator-5556",avd:"Expected",grpcPort:8556}]}),
 run:async()=>({stdout:"Other\nOK"}),build:async()=>{built=true}}),/AVD mismatch/);
 assert.equal(built,false);
});

import { EventEmitter } from "node:events";
import { Duplex, PassThrough } from "node:stream";
import { ScrcpySession } from "../plugins/autonom/skills/android-emulator-browser/scripts/scrcpy-session.mjs";
test("host failure falls back once to Android and stop tears down both",async()=>{
 const commands=[];let host,controlWrites=0,attemptConnections=0,server;
 const metadata=Buffer.concat([Buffer.alloc(64),Buffer.from([0x68,0x32,0x36,0x34])]);
 const spawn=(_file,args)=>{
  commands.push(args.at(-1));attemptConnections=0;
  server=new EventEmitter();server.stdout=new PassThrough();server.stderr=new PassThrough();
  server.kill=()=>queueMicrotask(()=>server.emit("exit",0,null));return server;
 };
 const connect=()=>{
  const isFirst=attemptConnections++===0;
  const socket=new Duplex({read(){},write(_b,_e,cb){controlWrites++;cb()}});
  socket.setNoDelay=()=>{};
  const owner=server;
  socket.once("close",()=>owner.emit("exit",0,null));
  queueMicrotask(()=>{socket.emit("connect");
   if(isFirst)socket.push(Buffer.concat([Buffer.from([0]),commands.at(-1).includes("video=false")?Buffer.alloc(64):metadata]));
  });return socket;
 };
 const session=new ScrcpySession({adbPath:"fake",serial:"emulator-5556",serverPath:"fake",maxSize:1280,maxFps:60,bitRate:12000000,
  exec:async(_file,args)=>({stdout:args.includes("forward")?"43210":""}),spawn,connect,
  hostFactory:async()=>()=>{
   const stdout=new PassThrough();
   host={stdout,keys:0,stopped:false,stats:{received:0},requestKeyFrame(){this.keys++},
    async stop(){this.stopped=true;stdout.destroy()}};
   queueMicrotask(()=>stdout.write(metadata));return host;
  }});
 session.on("error",()=>{});
 try {
  await session.start();assert.equal(session.stats.videoBackend,"videotoolbox");
  session.requestKeyFrame();assert.equal(host.keys,1);assert.equal(controlWrites,0);
  host.stdout.destroy();
  await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>reject(Error("fallback timed out")),4000);
   session.on("state",state=>{if(state==="streaming"&&session.stats.videoBackend==="android"){clearTimeout(timer);resolve()}});
  });
  assert.equal(commands.length,2);assert.match(commands[0],/video=false/);assert.doesNotMatch(commands[1],/video=false/);
  assert.equal(host.stopped,true);assert.match(session.stats.hostFallback,/Android encoder/);
 } finally {await session.stop()}
 assert.equal(session.state,"stopped");
});

test("discovery authenticates the exact AVD, serial and API port, without console tokens",()=>{
 const config={serial:"emulator-5556",avd:"Example",grpcPort:8556};
 const record="port.serial=5556\ngrpc.port=8556\navd.id=Example\ngrpc.token=synthetic-ephemeral-token\n";
 assert.equal(discoveryRecord(record,config),"synthetic-ephemeral-token");
 assert.equal(discoveryRecord(record,{...config,grpcPort:8554}),null);
 assert.equal(discoveryRecord(record,{...config,avd:"Other"}),null);
 assert.equal(discoveryRecord(record+"grpc.server_cert=/private/cert",config),null);
 assert.equal(discoveryRecord(record.replace("synthetic-ephemeral-token",""),config),null);
});

test("frame pacing tolerates delivery jitter while bounding sustained bursts",()=>{
 const p=new FramePacer(60);
 for(let i=0;i<120;i++)assert.equal(p.accept(i*1000/60+(i%2?3:0)),true);
 const fast=new FramePacer(60);let accepted=0;for(let t=0;t<1000;t++)accepted+=Number(fast.accept(t));
 assert.ok(accepted>=60&&accepted<=61);
});

test("cutout override needs explicit opt-in and restores its sole resource",async()=>{
 const calls=[];let enabled=false;
 const run=async(file,args)=>{
   calls.push(args.slice(2));
   const c=args.slice(2).join(" ");
   if(c==="shell cmd overlay list android")return {stdout:""};
   if(c.includes("overlay lookup"))return {stdout:enabled?"false":"true"};
   if(c.includes("overlay enable "))enabled=true;
   if(c.includes("overlay disable "))enabled=false;
   return {stdout:""};
 };
 await (await prepareCutout({config:{serial:"emulator-5556"},adbPath:"adb",run}))();
 assert.equal(calls.length,0);
 const restore=await prepareCutout({config:{serial:"emulator-5556",manageCutout:true},adbPath:"adb",run});
 assert.equal(enabled,true);
 assert.equal(calls.filter(c=>c.includes("fabricate")).length,1);
 assert.ok(calls.some(c=>c.includes("android:bool/config_fillMainBuiltInDisplayCutout")));
 await restore.refresh();assert.equal(enabled,true);
 assert.equal(calls.filter(c=>c.includes("disable")).length,0);
 enabled=false;await restore.refresh();assert.equal(enabled,true);
 assert.equal(calls.filter(c=>c.includes("enable")).length,2);
 await restore();assert.equal(enabled,false);
});
test("stop during decoration setup restores before completion without opening capture",async()=>{
 let release,restored=0;
 const setup=new Promise(r=>release=r);
 const host=new HostVideo({binary:"/bin/cat",maxFps:60,bitRate:12000000,
   decorate:async()=>{await setup;return async()=>{restored++}}});
 const stop=host.stop();release();await stop;
 assert.equal(restored,1);assert.equal(host.client,undefined);
 await host.stop();assert.equal(restored,1);
});

test("hidden tabs keep decorations stable; detach and capture failure restore once",async()=>{
 const lease=new CaptureDecorations();let created=0,restored=0;
 const create=async()=>{created++;return async()=>{restored++}};
 await (await lease.acquire(create))(false);
 await (await lease.acquire(create))(false);
 assert.equal(created,1);assert.equal(restored,0);
 await lease.restore();await lease.restore();assert.equal(restored,1);
 await (await lease.acquire(create))(true);assert.equal(created,2);assert.equal(restored,2);
});

test("late transport errors after normal stop do not invalidate the decoration lease",async()=>{
 let release,failed;
 const setup=new Promise(r=>release=r);
 const host=new HostVideo({binary:"/bin/cat",maxFps:60,bitRate:12000000,
  decorate:async()=>{await setup;return async value=>{failed=value}}});
 const stop=host.stop();host.abort();host.child.stdin.emit("error",Error("closed transport"));
 release();await stop;assert.notEqual(failed,true);
});
