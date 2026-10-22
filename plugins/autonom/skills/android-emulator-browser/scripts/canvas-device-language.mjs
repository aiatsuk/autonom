// Native selects are enhanced by the shared CanvasDropdown component.
export function deviceLanguageMarkup() {
  return '<div class="canvas-setting"><label for="device-language">Language</label><select id="device-language" aria-label="Device language" disabled><option value="">Loading…</option></select></div>'+
    '<div id="device-language-confirm" class="tools-confirm" hidden><p class="tools-confirm-text">Changing the device language restarts Simulator and closes running apps.</p><div class="tools-row"><button type="button" class="btn" id="device-language-cancel">Cancel</button><button type="button" class="btn primary" id="device-language-apply">Change &amp; restart</button></div></div>'+
    '<p id="device-language-message" class="hint" role="status" hidden></p>';
}
export function deviceLanguageScript() {
  return `
const languageSelect=$("device-language"),languageMessage=$("device-language-message"),languageConfirm=$("device-language-confirm");
let deviceLanguage=null,languageBusy=false,languageRead=0,languagePending=null;
const languageTags=["en","en-US","en-GB","ru-RU","de-DE","fr-FR","es-ES","it-IT","pt-BR","pt-PT","nl-NL","pl-PL","tr-TR","uk-UA","ar","he","hi-IN","ja-JP","ko-KR","zh-Hans","zh-Hant"];
function languageName(tag){try{return new Intl.DisplayNames(["en"],{type:"language"}).of(tag)||tag}catch{return tag}}
function languageNotice(value){languageMessage.textContent=value;languageMessage.hidden=!value}
function cancelDeviceLanguage(){languagePending=null;languageConfirm.hidden=true}
function syncLanguageControl(){if(languageSelect)languageSelect.disabled=languageBusy||!deviceLanguage||!canSendInput();const apply=$("device-language-apply");if(apply)apply.disabled=languageBusy||!canSendInput()}
function showLanguage(result){
  deviceLanguage=result.language;
  const tags=[...new Set([...languageTags,deviceLanguage])];
  languageSelect.replaceChildren(...tags.map(tag=>{const option=document.createElement("option");option.value=tag;option.textContent=languageName(tag);return option}));
  languageSelect.value=deviceLanguage;languageSelect.title=deviceLanguage;syncLanguageControl();
}
async function refreshDeviceLanguage(){
  if(!languageSelect||languageBusy)return;const seq=++languageRead;
  try{const reply=await post("/tools/call",{op:"device.language.get",payload:{}});if(seq!==languageRead)return;showLanguage(reply.result);languageNotice("")}
  catch(error){if(seq!==languageRead)return;deviceLanguage=null;languageSelect.replaceChildren();const option=document.createElement("option");option.textContent="Unavailable";option.value="";languageSelect.append(option);syncLanguageControl();languageNotice(error.message)}
}
async function applyDeviceLanguage(language,restart=false){
  if(languageBusy||!canSendInput())return;
  languageBusy=true;++languageRead;languageConfirm.hidden=true;syncLanguageControl();languageNotice(restart?"Restarting Simulator…":"Applying…");
  try{const reply=await post("/tools/call",{op:"device.language.set",payload:{language,restart}});showLanguage(reply.result);languageNotice("")}
  catch(error){languageSelect.value=deviceLanguage;languageNotice(error.message);note(error.message)}
  finally{languageBusy=false;languagePending=null;syncLanguageControl()}
}
if(languageSelect){
  languageSelect.addEventListener("change",()=>{const chosen=languageSelect.value;languageSelect.value=deviceLanguage;if(!chosen||chosen===deviceLanguage||languageBusy||!canSendInput())return;if(PLATFORM==="ios"){languagePending=chosen;languageConfirm.hidden=false;$("device-language-apply").textContent="Use "+languageName(chosen)+" & restart"}else applyDeviceLanguage(chosen)});
  $("device-language-cancel").addEventListener("click",()=>{cancelDeviceLanguage();languageSelect.focus()});
  $("device-language-apply").addEventListener("click",()=>{if(languagePending)applyDeviceLanguage(languagePending,true)});
}
`;
}
