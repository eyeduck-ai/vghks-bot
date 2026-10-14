"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.value="";this.events={};this.className="";this.ownText="";this.classList={add:name=>{this.className+=" "+name;}};}
 set textContent(value){this.ownText=String(value);this.children=[];}get textContent(){return this.ownText+this.children.map(child=>child.textContent).join(" ");}
 append(...values){this.children.push(...values);}prepend(...values){this.children.unshift(...values);}replaceChildren(...values){this.ownText="";this.children=[...values];}
 setAttribute(name,value){this[name]=value;}addEventListener(name,run){this.events[name]=run;}
 all(){return this.children.flatMap(child=>[child,...child.all()]);}
 querySelector(selector){return this.all().find(child=>child.tagName===selector)||null;}
}
const node=(tag,text,cls)=>{const result=new Element(tag);if(text!==undefined)result.textContent=text;if(cls)result.className=cls;return result;};
const context={window:{},node,Node:Element};vm.createContext(context);
const bot=fs.readFileSync("vghks_bot/static/bot.js","utf8"),start=bot.indexOf("function table(");
vm.runInContext(bot.slice(start,bot.indexOf("\n",start)),context);
vm.runInContext(fs.readFileSync("vghks_bot/static/registration-history.js","utf8"),context);
const container=node("div"),rows=[
 {visit_date:"2026-10-15",section_code:"70",section_name:"眼科",room:"02",sequence_no:"001",expected_time:"09:30",registered_at:"115/10/01 08:30",registered_by:"MWEB",no_show:null,notes:'<img src=x onerror="alert(1)">',columns:{"新增欄位":"保留值","空欄":""}},
 {visit_date:"2026-10-14",sequence_no:"002",no_show:null},
 {visit_date:"2026-10-10",sequence_no:"007",status:"院方狀態",no_show:true,registered_by:"OPD_SYN",notes:"回診"},
 {visit_date:"2026-10-10",sequence_no:"007",status:"院方狀態",no_show:null,registered_by:"OPD_SYN",notes:"回診"},
 {visit_date:"2026-10-09",sequence_no:"010",status:"來源取消狀態",cancelled_at:"115/10/08 09:00",cancelled_by:"STAFF_SYN",no_show:false},
 {visit_date:null,sequence_no:"011",no_show:"true",columns:{"日期原文":"不明"}},
];
const original=JSON.stringify(rows);
context.window.RegistrationHistoryUI.append(container,{payload:rows,queried_on:"2026-10-14",registration_schema:1},"2026-10-15");
const all=()=>container.all(),summary=()=>all().find(element=>element.role==="status").textContent;
const headings=()=>all().filter(element=>element.tagName==="h3").map(element=>element.textContent);
assert.deepEqual(headings(),["今天及未來（2）","過去（3）","日期不明（1）"]);
assert.match(summary(),/顯示 6 \/ 6 筆.*未到診 1 筆.*未知 5 筆/);
const originalFields=all().find(element=>element.tagName==="details");
assert.equal(originalFields.children.length,1,"closed original fields eagerly built a large table");
originalFields.open=true;originalFields.events.toggle();originalFields.events.toggle();
assert.equal(originalFields.children.length,2,"reopening original fields duplicated its table");
for(const value of ["預計 09:30","70 眼科","診間 02","001","115/10/01 08:30","MWEB","來源取消狀態","115/10/08 09:00","STAFF_SYN","新增欄位","保留值","（空白）",'<img src=x onerror="alert(1)">'])assert.ok(container.textContent.includes(value),value);
assert.equal(all().filter(element=>element.tagName==="img").length,0,"source text became executable markup");
assert.ok(all().filter(element=>element.tagName==="summary").length>=2);
assert.ok(all().filter(element=>element.role==="region").every(element=>element.tabIndex===0));
assert.ok(all().filter(element=>element.tagName==="th").every(element=>element.scope==="col"));
const filter=all().find(element=>element.tagName==="select"),query=all().find(element=>element.tagName==="input");
filter.value="no-show";filter.events.change();assert.match(summary(),/顯示 1 \/ 6 筆.*未知 0 筆/);
assert.ok(!container.textContent.includes("來源取消狀態"));
filter.value="unknown";filter.events.change();assert.match(summary(),/顯示 5 \/ 6 筆.*未到診 0 筆.*未知 5 筆/);
filter.value="all";query.value="opd_syn";query.events.input();assert.match(summary(),/顯示 2 \/ 6 筆/);
query.value="保留值";query.events.input();assert.match(summary(),/顯示 1 \/ 6 筆/);
query.value="NO_MATCH";query.events.input();assert.match(summary(),/顯示 0 \/ 6 筆/);assert.ok(container.textContent.includes("沒有符合紀錄"));
assert.equal(JSON.stringify(rows),original,"display changed the saved source payload");
const legacy=node("div");context.window.RegistrationHistoryUI.append(legacy,{payload:[{sequence_no:"003"}]},"2026-10-14");
assert.match(legacy.textContent,/舊版保存資料/);assert.match(legacy.textContent,/未知 1 筆/);assert.match(legacy.textContent,/日期不明（1）/);
const empty=node("div");context.window.RegistrationHistoryUI.append(empty,{payload:[],registration_schema:1},"2026-10-14");
assert.match(empty.textContent,/顯示 0 \/ 0 筆/);assert.match(empty.textContent,/沒有紀錄/);assert.doesNotMatch(empty.textContent,/舊版保存資料/);
assert.ok(fs.readFileSync("vghks_bot/static/bot.html","utf8").includes('src="/registration-history.js"'));
console.log("Registration history UI: source status, unknown attendance, raw fields, filters and safe text passed.");
