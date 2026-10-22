// Geometry comes from the target-bound server descriptor, never from aspect-based model guesses.
export function deviceFrameStyles() {
  return `
.device{padding:0;border-radius:0;background:transparent;box-shadow:none}
.surface{left:var(--screen-x,0%);top:var(--screen-y,0%);width:var(--screen-w,100%);height:var(--screen-h,100%);border-radius:0}
.device-frame-layer{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:1;user-select:none}

.device[data-frame=none]{background:transparent;border-radius:0;box-shadow:none}
.device[data-frame=none] .device-frame-layer{display:none}
.frame-source{color:var(--text-2);font-size:12px;text-align:right}
`;
}
export function deviceFrameScript() {
  return `
const frameControl=$("device-frame-style"),frameLabel=$("device-frame-source"),frameLayer=$("device-frame-layer");
const framePreferenceKey="autonom.canvas.frame."+(DEVICE_ID||PLATFORM);
let framePreference="auto",frameRevision=0,frameKey="",frameGeometry=null,frameSize=null;
const frameCache=new Map();
try{if(localStorage.getItem(framePreferenceKey)==="none"||(!localStorage.getItem(framePreferenceKey)&&localStorage.getItem("autonom.canvas.frame."+PLATFORM)==="none"))framePreference="none"}catch{}
function selectedFramePreset(){return typeof view!=="undefined"&&PLATFORM==="android"?view.preset||"default":"default"}
function captureFrameOptions(){return framePreference==="none"||!frameGeometry?{}:{frame:{mode:"auto",rotation:frameGeometry.rotation,preset:selectedFramePreset()}}}
function applyFrameGeometry(desc,width,height){
  const unavailable=desc&&desc.source==="generic";
  const unavailableModel=unavailable?desc.model:null,reason=desc&&desc.fallbackReason||"";
  if(unavailable)desc=null;
  frameGeometry=desc;
  const body=desc?desc.bodySize:{width,height},screen=desc?desc.screenRect:{x:0,y:0,width,height};
  device.setAttribute("data-frame",framePreference);
  device.setAttribute("data-frame-source",desc?desc.source:"none");
  device.style.aspectRatio=body.width+" / "+body.height;
  device.style.setProperty("--video-ratio",String(body.width/body.height));
  for(const [key,value] of [["x",screen.x/body.width],["y",screen.y/body.height],["w",screen.width/body.width],["h",screen.height/body.height]])device.style.setProperty("--screen-"+key,value*100+"%");
  device.style.setProperty("--body-radius",desc&&desc.borderRadius?desc.borderRadius/body.width*100+"% / "+desc.borderRadius/body.height*100+"%":"0");
  const mask=desc&&desc.screenMask?'url("'+desc.screenMask.image+'")':"none";
  for(const surface of [video,image]){surface.style.setProperty("mask-image",mask);surface.style.setProperty("-webkit-mask-image",mask);surface.style.setProperty("mask-size","100% 100%");surface.style.setProperty("-webkit-mask-size","100% 100%")}
  if(frameLayer){const layer=desc&&desc.layers[0];frameLayer.hidden=!layer;if(layer&&frameLayer.getAttribute("src")!==layer.image)frameLayer.setAttribute("src",layer.image)}
  if(frameLabel){frameLabel.textContent=framePreference==="none"?"":unavailable?"Frame unavailable for "+unavailableModel:desc?desc.model:"Frame unavailable";frameLabel.title=reason;frameLabel.hidden=framePreference==="none"}
}
function invalidateDeviceFrame(){frameCache.clear();frameKey="";if(frameSize)refreshDeviceFrame(frameSize.width,frameSize.height)}
async function refreshDeviceFrame(width,height){
  if(!width||!height)return;
  frameSize={width,height};
  const key=framePreference+":"+selectedFramePreset()+":"+width+"x"+height+":"+rotation;
  if(key===frameKey)return;frameKey=key;
  const revision=++frameRevision;
  if(framePreference==="none"){applyFrameGeometry(null,width,height);return}
  if(frameCache.has(key)){applyFrameGeometry(frameCache.get(key),width,height);return}
  // Never stretch the previous orientation's camera/mask over a new screen.
  applyFrameGeometry(null,width,height);
  if(frameLabel){frameLabel.textContent="";frameLabel.hidden=true}
  try{
    const response=await post("/tools/call",{op:"frame.describe",payload:{width,height,rotation,preset:selectedFramePreset()}});
    const desc=response.result;
    if(!desc||!desc.bodySize||!desc.screenRect)throw new Error("Frame descriptor unavailable");
    frameCache.set(key,desc);
    if(frameCache.size>8)frameCache.delete(frameCache.keys().next().value);
    if(revision===frameRevision)applyFrameGeometry(desc,width,height);
  }catch{
    if(revision===frameRevision)applyFrameGeometry(null,width,height);
  }
}
if(frameControl){frameControl.checked=framePreference!=="none";frameControl.addEventListener("change",()=>{framePreference=frameControl.checked?"auto":"none";try{localStorage.setItem(framePreferenceKey,framePreference)}catch{}frameKey="";if(frameSize)refreshDeviceFrame(frameSize.width,frameSize.height)})}
`;
}
