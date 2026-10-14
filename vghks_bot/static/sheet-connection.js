"use strict";
// Only public account labels/connection metadata enter the DOM. Credentials
// are copied directly between account databases by the local server.
window.SheetConnectionPicker={mount({container,call,onCopy,context=()=>"",readOnly=()=>false,perform=task=>task()}){
 const make=(tag,text)=>{const value=document.createElement(tag);if(text!==undefined)value.textContent=text;return value;};
 const fieldset=make("fieldset"),legend=make("legend","從其他帳號帶入"),label=make("label","來源帳號"),select=make("select"),copy=make("button","帶入並保存連線設定"),status=make("p");
 select.setAttribute("aria-label","刀表連線來源帳號");copy.type="button";status.className="caption";status.setAttribute("role","status");status.setAttribute("aria-live","polite");
 label.append(select);fieldset.append(legend,make("p","複製來源帳號的刀表目標與服務帳戶；兩邊之後可獨立調整，本帳號院區保持原設定。"),label,copy,status);container.append(fieldset);
 let sources=new Map(),revision=0,busy=false;
 function controls(){select.disabled=busy||readOnly()||!sources.size;copy.disabled=busy||readOnly()||!sources.has(select.value);}
 function reset(){revision++;sources.clear();select.replaceChildren(new Option("請選擇來源帳號",""));status.textContent="";busy=false;controls();}
 async function load(){
  reset();const version=revision,key=context();busy=true;status.textContent="正在讀取可帶入的帳號…";controls();
  try{
   const value=await call("google/sources");if(version!==revision||key!==context())return;
   if(!Array.isArray(value.sources))throw new Error("帳號連線清單無法辨識，請重新開啟設定。");
   sources=new Map(value.sources.map(source=>[source.id,source]));
   select.append(...value.sources.map(source=>new Option([source.label||source.username,source.campus,source.username].filter(Boolean).join(" · "),source.id)));
   status.textContent=sources.size?"選擇來源後可查看刀表與服務帳戶，再按帶入保存。":"尚無其他已設定刀表與服務帳戶的帳號。";
  }catch(error){if(version===revision&&key===context())status.textContent=error.message;}
  finally{if(version===revision&&key===context()){busy=false;controls();}}
 }
 select.addEventListener("change",()=>{const source=sources.get(select.value);status.textContent=source?["刀表："+source.spreadsheet_id,"服務帳戶："+source.email].join(" · "):"請選擇來源帳號。";controls();});
 async function bring(){
  if(busy||readOnly()||!sources.has(select.value))return;
  const source=sources.get(select.value),version=revision,key=context();busy=true;status.textContent="正在帶入並保存連線設定…";controls();
  try{const value=await call("google/copy",{source_account_id:source.id});if(version!==revision||key!==context())return;await onCopy(value);status.textContent="已帶入並保存。可使用「保存並測試讀取」確認連線。";}
  catch(error){if(version===revision&&key===context())status.textContent=error.message;throw error;}
  finally{if(version===revision&&key===context()){busy=false;controls();}}
 }
 copy.addEventListener("click",async()=>{try{await perform(bring);}catch{/* bring or the parent surface already displays the error for its original context. */}});
 reset();return {load,reset};
}};
