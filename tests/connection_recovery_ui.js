"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync("vghks_bot/static/bot.js","utf8");
const operation=source.match(/async function maybeRecoverConnection\(\)\{[\s\S]*?\n\}/)[0];
const requests=[],dialogs=[],messages=[],status={textContent:""};
const context={account:"test",root:{read_only:false},work:{online:false,offline_mode:false,tasks:[],connection_error_code:"AUTH_LOGIN_REJECTED"},
  recoveringConnection:false,reconnectAttempts:new Map(),resumedTasks:new Set(),inProgress:new Set(["queued","running","cancelling"]),
  Date,$:()=>status,interruptedTask:()=>({id:"failed-input"}),openReconnect:(...args)=>dialogs.push(args),
  ra:async path=>{requests.push(path);return {status:"unavailable",message:"連線暫時中斷"};},say:(...args)=>messages.push(args),
  renderAccounts(){},refresh:async()=>{},api:async()=>{}};
vm.createContext(context);
vm.runInContext(operation+"\nglobalThis.recover=maybeRecoverConnection;",context);
(async()=>{
  await context.recover();await context.recover();
  assert.equal(requests.length,0,"a known credential rejection must not trigger another login");
  assert.equal(dialogs.length,1,"show the password prompt once");
  assert.equal(dialogs[0][0],"failed-input","preserve the affected task for resume");
  assert.equal(context.reconnectAttempts.get("test").blocked,true);
  assert.equal(context.recoveringConnection,false);
  context.work.connection_error_code="NETWORK_TIMEOUT";
  context.reconnectAttempts.clear();
  await context.recover();await context.recover();
  assert.deepEqual(requests,[],"polling must not resend passwords for a network fault");
  assert.equal(dialogs.length,1,"network outages do not request different credentials");
  assert.equal(context.reconnectAttempts.get("test").blocked,true);
  assert.equal(context.recoveringConnection,false);
  for(const [code,action] of [["PORTAL_PASSWORD_CHANGE_REQUIRED","password_change"],["AUTH_RELOGIN_FAILED","network"],["AUTH_NOT_AUTHENTICATED","login"]]){
    context.work.connection_error_code=code;context.work.connection_issue={action};context.reconnectAttempts.clear();
    const count=dialogs.length;
    await context.recover();await context.recover();
    assert.equal(requests.length,0,"SDK recovery failure must never replay a login from UI polling");
    assert.equal(dialogs.length,count+(action==="network"?0:1));
    if(action==="password_change")assert.match(dialogs.at(-1)[2],/院方入口變更/);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
