"use strict";
// Verify rendered clinical signs and date labels without any patient or hospital I/O.
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
class Element {
  constructor(tag) {this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.value="";}
  set textContent(value) {this.children=[];this.text=String(value);}
  get textContent() {return (this.text||"")+this.children.map(child=>child.textContent).join("");}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.text="";this.children=children;}
  setAttribute(name,value) {this[name]=String(value);}
  addEventListener() {}
  querySelectorAll(selector) {return this.children.flatMap(child=>[
    ...(child.tagName===selector.toUpperCase()?[child]:[]),...child.querySelectorAll(selector)]);}
}
const node=(tag,text,cls)=>{const result=new Element(tag);if(text!==undefined)result.textContent=text;if(cls)result.className=cls;return result;};
const context={document:{createElement:tag=>node(tag),createTextNode:text=>node("#text",text)},window:{addEventListener(){}},Intl};
vm.createContext(context);
for(const file of ["cataract-numeric.js","clinical-ui.js"])vm.runInContext(fs.readFileSync("vghks_bot/static/"+file,"utf8"),context);
const numeric=context.window.CataractNumeric;
for(const [input,exam,metric,expected] of [
  ["1.25 -0.50 X 90","驗光-散瞳前","OD","+1.25 -0.50 X 90"],
  ["2.25 1.00 × 175","配鏡","OS","+2.25 +1.00 × 175"],
  ["−1.25 0.00 x 0","驗光-散瞳後","OD","-1.25 0.00 x 0"],
  ["＋２．００ ０．５０ X １８０","驗光-散瞳前","OS","+2.00 +0.50 X 180"],
  ["1.50","驗光-散瞳後","OD / SPH (D)","+1.50"],
  [".75","配鏡","OS CYL (D)","+.75"],
  ["1.125","驗光-散瞳前","SE","+1.125"],
  ["1.25","驗光-散瞳前 / SPH / OD","2026-09-11","+1.25"],
  ["43.25","KM / OD K1","2026-09-11","43.25"],
  ["+1.50","配鏡","Sphere","+1.50"],
  ["-0.50","配鏡","Cylinder","-0.50"],
  ["0.00","配鏡","SPH","0.00"],
  ["90","配鏡","Axis (°)","90"],
  ["43.25","KM","OD K1 (D)","43.25"],
  ["K1 41.50 8.14 X 45 K2 42.25 7.98 X 135 CYL 0.75 X 45","KM","OS",
    "K1 41.50 8.14 X 45 K2 42.25 7.98 X 135 CYL +0.75 X 45"],
  ["0.8","Va","OD","0.8"],
  ["18","IOP-pneumo","OD","18"],
  ["HM","Va","OS","HM"],
  ["1.25 -0.50 X 181","驗光-散瞳前","OD","1.25 -0.50 X 181"],
  ["0.8 (1.25 -0.50 X 90)","配鏡","OS","0.8 (1.25 -0.50 X 90)"],
  ["","配鏡","SPH",""], ["—","配鏡","SE","—"], ["Infinity","配鏡","SE","Infinity"],
])assert.equal(numeric.displayValue(input,exam,metric),expected);
function row(exam,kind,values,status="parsed") {
  return {date:"2026-09-11",exams:[exam],cells:[{exam,side:"OD",raw:"synthetic source"}],
    measurements:[{exam,side:"OD",kind,status,values}]};
}
const inputs=[
  row("驗光-散瞳前","refraction",{sph:2.25,cyl:1,se:2.75,axis:90}),
  row("驗光-散瞳後","refraction",{sph:1.25,cyl:-.5,se:1,axis:175}),
  row("配鏡","refraction",{sph:0,cyl:0,se:0,axis:0}),
  row("配鏡","refraction",{sph:null,cyl:-.25,se:null,axis:90},"partial"),
  row("KM","keratometry",{k1:41.25,r1:8.2,axis1:160,k2:42.5,r2:7.96,axis2:70,kavg:41.875,cyl:1.25,cyl_axis:160}),
];
const snapshot=JSON.stringify(inputs),rendered=numeric.create(inputs);
const output=rendered.querySelectorAll("td").map(cell=>cell.textContent);
for(const value of ["+2.25 +1.00 X 90","SE +2.75","+1.25 -0.50 X 175","SE +1","0.00 0.00 X 0","SE 0","— -0.25 X 90","SE —",
  "K1 41.25 (8.2) X 160","K2 42.50 (7.96) X 70","Kavg 41.875 | CYL +1.25 X 160"])assert.ok(output.includes(value),value);
const badges=rendered.querySelectorAll("span").filter(element=>element.className==="measurement-label").map(element=>element.textContent);
for(const label of ["SE","K1","K2","Kavg","CYL"])assert.ok(badges.includes(label),label);
assert.equal(JSON.stringify(inputs),snapshot,"display changed the saved values");
assert.equal(numeric.labelledText("<script> SE +1.50</script>").textContent,"<script> SE +1.50</script>");

// Exercise the actual review table entry point, including its conservative alignment fallback.
context.CataractNumeric=numeric;context.node=node;context.empty=text=>node("p",text);
context.table=(headers,rows)=>{
  const table=node("table");for(const header of headers)table.append(node("th",header));
  for(const row of rows)for(const value of row){const td=node("td");td.append(value);table.append(td);}return table;
};
const bot=fs.readFileSync("vghks_bot/static/bot.js","utf8");
vm.runInContext(bot.slice(bot.indexOf("const eyeNumericPatterns="),bot.indexOf("function renderExtension(")),context);
const review=node("div"), tables=[
  {title:"驗光-散瞳前",headers:["日期","OD","OS"],rows:[["2026-09-11","1.25 -0.50 X 90","-1.50 0.50 X 175"]]},
  {title:"配鏡",headers:["SPH (D)","CYL (D)","SE (D)"],rows:[["2.00","0.50","2.25"]]},
  {title:"Va",headers:["OD","OS"],rows:[["0.8","HM"]]},
  {title:"驗光-散瞳前",headers:["項目","OD","OS"],rows:[["Sph","3.00","-2.00"],["Cyl","1.00","0.00"]]},
  {title:"驗光-散瞳前 / SE / OD",headers:["2026-08-14","2026-09-11"],rows:[["1.125","-1.75"]]},
  {title:"配鏡",headers:["SPH","CYL"],header_rows:[["ambiguous"]],rows:[["1.00","2.00"]]},
];
const saved=JSON.stringify(tables);context.appendNumericTables(review,tables);
const reviewValues=review.querySelectorAll("td").map(cell=>cell.textContent);
for(const value of ["+1.25 -0.50 X 90","-1.50 +0.50 X 175","+2.00","+0.50","+2.25","0.8","HM","1.00","2.00","+3.00","-2.00","+1.00","0.00","+1.125"])assert.ok(reviewValues.includes(value),value);
assert.equal(JSON.stringify(tables),saved,"review changed original tables");

const name=context.window.ClinicalUI.comparisonName;
const file=(digest,date,selectionOrder,fileIndex=1,fileCount=1)=>({digest,date,selectionOrder,fileIndex,fileCount});
const old=file("old","2026-08-14",1),recent=file("recent","2026-09-11",2),second=file("second","2026-09-11",3,2,2);
assert.equal(name(old,[old,recent]),"2026-08-14");
assert.equal(name(recent,[old,recent]),"2026-09-11");
assert.equal(name(second,[second]),"2026-09-11 · 檔案 2","known multiple attachments lost their source ordinal");
assert.equal(name(recent,[recent,old,second]),"2026-09-11 · 檔案 1");
assert.equal(name(second,[second,old,recent]),"2026-09-11 · 檔案 2","moving panes renumbered the files");
assert.equal(name(old,[second,old,recent]),"2026-08-14");
const originalFirst={...recent,fileCount:2,reportId:"one-report",selectionOrder:5};
const originalSecond={...second,reportId:"one-report",selectionOrder:4};
assert.equal(name(originalFirst,[originalSecond,originalFirst]),"2026-09-11 · 檔案 1","reverse selection lost original file numbering");
assert.equal(name(originalSecond,[originalFirst,originalSecond]),"2026-09-11 · 檔案 2");
assert.equal(name(file("unknown","",4)),"日期未提供");
console.log("Signed ophthalmic values, measurement badges, conservative review tables and stable comparison date labels passed.");
