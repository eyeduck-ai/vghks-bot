"use strict";
// Keep source status, explicit attendance evidence and account codes separate.
window.RegistrationHistoryUI=(()=>{
 const text=value=>value===null||value===undefined?"":String(value);
 function lines(main,detail){const box=node("div",undefined,"registration-lines");box.append(node("span",main||"未提供"));if(detail)box.append(node("span",detail,"caption"));return box;}
 function attendance(row){return row.no_show===true?"來源標示未到診":"未知";}
 function status(row){
  const box=lines(row.status||"未提供");
  if(row.cancelled_at)box.append(node("span","已取消","registration-cancelled"));
  return box;
 }
 function actions(row){
  const box=node("div",undefined,"registration-lines");
  box.append(node("span","掛號："+(row.registered_at||"時間未提供")),node("span","人員："+(row.registered_by||"未提供"),"caption"));
  if(row.cancelled_at||row.cancelled_by)box.append(node("span","取消："+(row.cancelled_at||"時間未提供")),node("span","人員："+(row.cancelled_by||"未提供"),"caption"));
  return box;
 }
 function notes(row){
  const box=node("div",undefined,"registration-notes");box.append(node("span",row.notes||"未提供"));
  const fields=Object.entries(row.columns||{});
  if(fields.length){
   const details=node("details",undefined,"registration-original");
   details.append(node("summary","原始欄位（"+fields.length+"）"));
   let loaded=false;details.addEventListener("toggle",()=>{
    if(details.open&&!loaded){details.append(table(["欄位","原值"],fields.map(([key,value])=>[key,text(value)||"（空白）"])));loaded=true;}
   });
   box.append(details);
  }
  return box;
 }
 function append(container,data,fallbackDay){
  const day=data.queried_on||data.updated_at?.slice(0,10)||fallbackDay;
  const rows=(Array.isArray(data.payload)?data.payload:[]).filter(row=>row&&typeof row==="object")
   .slice().sort((a,b)=>text(b.visit_date).localeCompare(text(a.visit_date)));
  container.append(node("p","查詢基準日 "+day,"caption"));
  container.append(node("p","未到診僅依院方明確標記；未知不代表已到診。掛號人員保留原始代碼，尚無自行掛號／醫師代掛的分類對照。","caption"));
  if(data.registration_schema!==1)container.append(node("p","這是舊版保存資料，可能缺少未到診標記；登入後更新資料可取得完整欄位。","registration-legacy"));
  const controls=node("div",undefined,"registration-filters"),queryLabel=node("label","搜尋掛號紀錄"),query=node("input");
  query.type="search";query.placeholder="日期、科別、序號、人員、註記";queryLabel.append(query);
  const filterLabel=node("label","到診標記"),filter=node("select");
  for(const [value,label] of [["all","全部"],["no-show","來源標示未到診"],["unknown","未知"]]){const option=node("option",label);option.value=value;filter.append(option);}filter.value="all";filterLabel.append(filter);
  controls.append(queryLabel,filterLabel);container.append(controls);
  const summary=node("p",undefined,"caption"),groups=node("div",undefined,"registration-history");summary.setAttribute("role","status");container.append(summary,groups);
  function render(){
   const term=query.value.trim().toLocaleLowerCase(),selected=rows.filter(row=>
    (filter.value==="all"||(filter.value==="no-show"?row.no_show===true:row.no_show!==true))&&
    (!term||[...Object.entries(row).filter(([key])=>key!=="columns").map(([,value])=>text(value)),
     ...Object.entries(row.columns||{}).flatMap(([key,value])=>[key,text(value)])].join(" ").toLocaleLowerCase().includes(term)));
   const noShow=selected.filter(row=>row.no_show===true).length;
   summary.textContent=`顯示 ${selected.length} / ${rows.length} 筆 · 來源標示未到診 ${noShow} 筆 · 未知 ${selected.length-noShow} 筆`;
   groups.replaceChildren();
   for(const [name,subset] of [["今天及未來",selected.filter(row=>row.visit_date&&row.visit_date>=day)],
    ["過去",selected.filter(row=>row.visit_date&&row.visit_date<day)],["日期不明",selected.filter(row=>!row.visit_date)]]){
    if(name==="日期不明"&&!subset.length)continue;
    groups.append(node("h3",name+"（"+subset.length+"）"));
    if(!subset.length){groups.append(node("p",rows.length?"沒有符合紀錄":"沒有紀錄","caption"));continue;}
    const listing=table(["日期／預計時間","科別／診間","序號","掛號狀態","到診標記","掛號／取消","註記／原始欄位"],subset.map(row=>[
     lines(row.visit_date||"未註明",row.expected_time?"預計 "+row.expected_time:"預計時間未提供"),
     lines([row.section_code,row.section_name].filter(Boolean).join(" "),"診間 "+(row.room||"未提供")),
     row.sequence_no||"未提供",status(row),node("span",attendance(row),row.no_show===true?"registration-no-show":"caption"),actions(row),notes(row)]));
    listing.classList.add("registration-table");listing.tabIndex=0;listing.setAttribute("role","region");listing.setAttribute("aria-label",name+"掛號紀錄，可橫向捲動");
    listing.querySelector("table").prepend(node("caption",name+"掛號紀錄","sr-only"));groups.append(listing);
   }
  }
  query.addEventListener("input",render);filter.addEventListener("change",render);render();
 }
 return {append};
})();
