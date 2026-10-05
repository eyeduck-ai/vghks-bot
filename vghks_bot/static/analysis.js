"use strict";

// Static shell only. All clinical/remote content below is inserted with textContent.
$("#analysisPane").innerHTML = [
  '<div class="section-heading"><div><h2 id="analysisTitle">分析工作區</h2></div><div class="actions"><button id="googleSettingsButton" type="button">刀表連線設定</button><button id="sheetHistoryButton" type="button">刀表更新紀錄</button></div></div>',
  '<div class="analysis-step-heading"><h3><span>1</span> 選擇病人</h3><div class="actions"><button id="analysisFromList" type="button">從門診清單選取</button><button id="analysisFromLibrary" type="button">從 tag／病歷搜尋選取</button></div></div>',
  '<div class="analysis-controls"><label>分析清單<select id="cohortSelect"></select></label><label>清單名稱<input id="cohortName" maxlength="100"></label><button id="saveCohort" type="button">儲存清單與帳號</button><button id="deleteCohort" type="button" class="quiet-button danger">移除清單</button></div>',
  '<details id="cohortDetails" class="analysis-members"><summary id="cohortSummary">抓取帳號</summary><div class="table-wrap"><table class="patient-table"><thead><tr><th scope="col">病人</th><th scope="col">病歷號</th><th scope="col">來源紀錄</th><th scope="col">抓取帳號</th><th scope="col">清單</th></tr></thead><tbody id="cohortMembers"></tbody></table></div></details>',
  '<div class="analysis-step-heading"><h3><span>2</span> 選擇模組與檢查期間</h3></div><div class="analysis-run-controls"><fieldset class="analysis-modules"><legend>執行模組</legend><label class="check-label"><input type="checkbox" name="analysisModule" value="retina" checked>視網膜</label><label class="check-label"><input type="checkbox" name="analysisModule" value="cataract">白內障</label><label class="check-label"><input type="checkbox" name="analysisModule" value="surgery">刀表更新</label></fieldset><label>檢查起日<input id="analysisStart" type="date" min="1912-01-01"></label><label>檢查迄日<input id="analysisEnd" type="date" min="1912-01-01"></label><label>資料來源<select id="analysisRefresh"><option value="cache">使用快取、補抓缺漏</option><option value="refresh">更新索引與缺少的報告</option><option value="force">強制重新抓取</option></select></label><button id="startAnalysis" type="button" class="primary">執行分析</button></div>',
  '<p class="small muted analysis-hint">日期留白：歷年資料。分析範圍與門診篩選日期分開。</p><div id="analysisRuns" aria-live="polite"></div>',
  '<div class="analysis-step-heading"><h3><span>3</span> 檢閱分析結果</h3></div><div id="cataractLayout" class="cataract-layout"><aside id="cataractPatients" class="cataract-patients" aria-label="術前分析病人清單" hidden></aside><div class="cataract-main"><div class="analysis-view-toolbar"><label>查看病人<select id="analysisPatient"></select></label><label>模組<select id="analysisView"><option value="retina">視網膜</option><option value="cataract">白內障術前分析</option><option value="surgery">刀表更新</option></select></label><button id="reloadAnalysis" type="button">檢閱本機資料</button><button id="cataractUpdate" type="button" hidden>更新歷年資料</button><button id="analysisScans" type="button" hidden>歷年掃描病歷</button><button id="analysisJSON" type="button">JSON ↓</button></div>',
  '<div id="analysisCoverage" class="small muted"></div><div id="analysisResults"><p class="empty-result">從門診清單或病歷管理選取病人</p></div></div></div>',
  '<div id="comparisonBar" class="comparison-bar" hidden><span id="comparisonCount"></span><button id="showComparison" type="button" class="primary">並排比較</button><button id="clearComparison" type="button">清除選取</button></div>',
  '<div id="surgeryWorkspace" hidden><div class="section-heading"><h3>手術候選</h3><div class="actions"><button id="reloadSurgery" type="button">重新整理候選</button><button id="previewSheet" type="button" class="primary">產生刀表差異預覽</button></div></div><div id="surgeryCandidates"></div><div id="sheetPreview"></div></div>'
].join("");
document.body.insertAdjacentHTML("beforeend", [
  '<dialog id="cohortDialog" aria-labelledby="cohortDialogTitle"><form id="createCohortForm" method="post"><div class="dialog-heading"><h2 id="cohortDialogTitle">建立分析清單</h2><button type="button" data-analysis-close="cohortDialog" aria-label="關閉">×</button></div><div class="record-body"><fieldset id="cohortAccountFields" hidden disabled><legend class="sr-only">選擇抓取帳號</legend><label>抓取帳號<select id="manualAccount" name="account_id" required></select></label><button id="manualAccountSettings" type="button" class="quiet-button">設定登入帳號</button></fieldset><label>清單名稱<input id="newCohortName" name="name" required maxlength="100"></label><p id="newCohortInfo" class="small muted"></p><p id="cohortError" class="form-error" role="alert" hidden></p><div class="dialog-actions"><button type="submit" class="primary">保存清單並選擇模組</button></div></div></form></dialog>',
  '<dialog id="comparisonDialog" class="wide-dialog" aria-labelledby="comparisonTitle"><div class="dialog-heading"><h2 id="comparisonTitle">歷次檢查比較</h2><button type="button" data-analysis-close="comparisonDialog" aria-label="關閉">×</button></div><div id="comparisonContent" class="comparison-columns"></div></dialog>',
  '<dialog id="googleDialog" aria-labelledby="googleTitle"><form id="googleForm" method="post"><div class="dialog-heading"><h2 id="googleTitle">刀表連線設定</h2><button type="button" data-analysis-close="googleDialog" aria-label="關閉">×</button></div><div class="record-body"><ol class="google-setup-steps"><li>在 Google Cloud 啟用 Sheets API，建立服務帳戶及 JSON 金鑰。<a href="https://developers.google.com/identity/protocols/oauth2/service-account#creatinganaccount" target="_blank" rel="noopener">設定說明 ↗</a></li><li>將刀表共用給金鑰中的 client_email，權限選「編輯者」。</li><li>匯入金鑰、儲存並測試讀取；之後在刀表差異預覽按「套用更新」。</li></ol><label>Google Sheets 網址或代碼<input id="googleSheetId" name="spreadsheet_id" required></label><label>服務帳戶 JSON 金鑰<input id="googleKeyFile" name="key_file" type="file" accept=".json,application/json"></label><p id="googleStatus" class="small muted"></p><p class="small muted">金鑰隨此帳號資料庫保存，換電腦不需重新匯入。</p><p id="googleConnectionResult" role="status" class="small"></p><div class="dialog-actions"><button id="removeGoogleKey" type="button" class="danger">移除本機金鑰</button><button type="submit">儲存連線設定</button><button id="testGoogleConnection" type="button" class="primary">儲存並測試讀取</button></div></div></form></dialog>',
  '<dialog id="sheetHistoryDialog" class="wide-dialog" aria-labelledby="sheetHistoryTitle"><div class="dialog-heading"><h2 id="sheetHistoryTitle">刀表更新紀錄</h2><button type="button" data-analysis-close="sheetHistoryDialog" aria-label="關閉">×</button></div><div id="sheetHistoryContent" class="record-body"></div></dialog>',
  '<dialog id="rawAnalysisDialog" class="wide-dialog" aria-labelledby="rawAnalysisTitle"><div class="dialog-heading"><h2 id="rawAnalysisTitle">原始資料與版本</h2><button type="button" data-analysis-close="rawAnalysisDialog" aria-label="關閉">×</button></div><div id="rawAnalysisContent" class="record-body"></div></dialog>'
].join(""));
Choices.enhance($("#analysisRefresh"));
const analysisNav = button("分析工作區", () => action(async () => { showPane("analysis"); await loadAnalysis(); }));
analysisNav.dataset.pane = "analysis"; analysisNav.setAttribute("aria-pressed", "false");
$('.workspace-nav [data-pane="history"]').before(analysisNav);
$("#listToAnalysis").addEventListener("click",()=>action(()=>beginCohort("list")));
$("#analysisFromList").addEventListener("click",()=>action(async()=>{showPane("list");await loadList();}));
$("#analysisFromLibrary").addEventListener("click",()=>action(async()=>{showPane("library");await loadLibrary();}));
$("#manualAccountSettings").addEventListener("click",()=>{
  $("#cohortDialog").close();$("#accountsDetails").open=true;
  const form=$$(".account-card").find(f=>f.dataset.id===$("#manualAccount").value)||$(".account-card");
  if(form)form.elements.username.focus();
});
const libraryEntry = el("div", undefined, "analysis-entry"), selectionText = el("span", "", "small muted");
selectionText.id = "analysisSelection";
libraryEntry.append(button("勾選病歷的病人 → 分析", () => action(() => beginCohort("library"))),
  button("全部篩選結果 → 分析（跨頁）", () => action(() => beginCohort("all"))), selectionText);
$("#libraryResults").before(libraryEntry);
$("#analysisJSON").before(button("原始資料／版本",()=>action(async()=>{
  if(!currentCohort)return;
  const data=await ap("raw",{cohort_id:currentCohort.id,mrn:$("#analysisPatient").value});
  const body=$("#rawAnalysisContent");body.replaceChildren();
  body.append(button("下載完整 JSON",()=>download("VGHKS-raw-"+data.member.mrn+".json",JSON.stringify(data,null,2),"application/json;charset=utf-8")));
  const groups=new Map();
  for(const row of data.versions) {
    if(!groups.has(row.key))groups.set(row.key,[]);groups.get(row.key).push(row);
  }
  for(const [key,versions] of groups) {
    const detail=el("details"), select=el("select"), pre=el("pre");
    detail.append(el("summary",key+" · "+versions.length+" 版"));
    select.setAttribute("aria-label",key+" 資料版本");
    select.append(...versions.map((v,i)=>new Option(localTime(v.saved_at),String(i))));
    select.value=String(versions.length-1);
    const render=()=>{pre.textContent=JSON.stringify(versions[Number(select.value)].payload,null,2);};
    select.addEventListener("change",render);render();detail.append(select,pre);body.append(detail);
  }
  if(!groups.size)body.append(el("p","尚無已保存資料","muted"));
  $("#rawAnalysisDialog").showModal();
})));

let analysisState = {cohorts:[], runs:[], modules:{}}, currentCohort = null, analysisResult = null;
let cohortSeed = null, resultRequest = 0, analysisRunSignature = "";
const compared = new Map(), surgeryEditors = new Map();
const ap = (path, body = {}) => api("/api/analysis/" + path, body);
$("#analysisScans").textContent="歷年掃描病歷";
$("#analysisScans").setAttribute("aria-label","歷年掃描病歷");
const compareMember = () => currentCohort?.members.find(member => member.mrn === $("#analysisPatient").value);
let scanPatient = "";
const dayText = value => value || "日期未註明";
const localTime = value => (value || "").slice(0,19).replace("T"," ");
const measureLabel = cell => [...new Set([cell.side||"側別未註明",cell.metric])].join(" · ");

async function beginCohort(source) {
  const fromTags=source==="tags"||source==="tags-all";
  $("#cohortAccountFields").hidden=!fromTags;$("#cohortAccountFields").disabled=!fromTags;
  $("#cohortError").hidden=true;
  $("#cohortDialogTitle").textContent="建立分析清單";
  if(fromTags) {
    const select=$("#manualAccount");
    select.replaceChildren(new Option("選擇登入帳號",""),...$$(".account-card").map(f=>new Option(
      f.elements.label.value||f.elements.username.value||"尚未設定帳號",f.dataset.id)));
    select.value=$("#listAccount").value||$$(".account-card")[0]?.dataset.id||"";
  }
  if(fromTags) {
    if(!tagGroupData?.total)throw new Error("此群組尚無符合的病人。");
    if(source==="tags"&&!tagSelected.size)throw new Error("請先勾選病人。");
    cohortSeed={source:"tags",all:source==="tags-all",filters:tagGroupFilters(),mrns:[...tagSelected].join("\n")};
    $("#newCohortInfo").textContent=(source==="tags-all"?"全部符合的 "+tagGroupData.total:"已勾選 "+tagSelected.size)+" 位病人；保存目前名單，之後修改 tag 不會更動此分析清單。";
  } else if (source === "list") {
    if (!listing) throw new Error("請先讀取門診清單。");
    const rows = listing.days.flatMap(d=>d.rows).filter(r=>selectedPatients.has(rowKey(r)));
    if (!rows.length) throw new Error("請先勾選病人。");
    cohortSeed = {source:"list",account_id:listing.account_id,rows:rows.map(r=>({id:r.id,day:r.day}))};
    $("#newCohortInfo").textContent = "已勾選 " + rows.length + " 筆門診；相同病歷號會合併。";
  } else {
    if (source !== "all" && !selectedRecords.size) throw new Error("請先勾選病歷。");
    if (source === "all" && !libraryData?.patients) throw new Error("沒有符合篩選的病人。");
    cohortSeed = {source:"library",all:source==="all",filters:libraryFilters(),record_ids:[...selectedRecords]};
    $("#newCohortInfo").textContent = source === "all" ? "保存全部篩選結果的 " + libraryData.patients + " 位病人。" : "相同病歷號會合併，保留勾選來源。";
  }
  $("#newCohortName").value = "分析清單 " + config.today;
  $("#cohortDialog").showModal();
}

async function loadAnalysis(preferred) {
  analysisState = await ap("cohorts");
  const chosen = preferred || $("#cohortSelect").value;
  $("#cohortSelect").replaceChildren(...analysisState.cohorts.map(c=>new Option(c.name+" · "+c.members.length+" 位",c.id)));
  if (!analysisState.cohorts.length) $("#cohortSelect").append(new Option("從門診清單或病歷管理選取病人",""));
  if (analysisState.cohorts.some(c=>c.id===chosen)) $("#cohortSelect").value=chosen;
  $("#analysisStart").max = $("#analysisEnd").max = config.today;
  selectCohort();
  renderAnalysisRuns();
  if (activePane === "analysis") await loadAnalysisResult();
}

function selectCohort() {
  currentCohort = analysisState.cohorts.find(c=>c.id===$("#cohortSelect").value) || null;
  $("#cohortName").value=currentCohort?.name || "";
  const previous = $("#analysisPatient").value;
  $("#analysisPatient").replaceChildren(...(currentCohort?.members||[]).map(m=>new Option((m.name||"未提供姓名")+" · "+m.mrn,m.mrn)));
  if (currentCohort?.members.some(m=>m.mrn===previous)) $("#analysisPatient").value=previous;
  $("#cohortSummary").textContent=(currentCohort?.members.length||0)+" 位病人 · 抓取帳號";
  $("#cohortMembers").replaceChildren(...(currentCohort?.members||[]).map(member=>{
    const tr=el("tr"), cell=el("td"), select=el("select"), remove=el("td");
    select.dataset.mrn=member.mrn; select.setAttribute("aria-label",member.mrn+" 抓取帳號");
    select.append(new Option("請選擇帳號",""),...[...accounts.values()].map(a=>new Option(a.label||a.username||"未設定",a.id)));
    select.value=member.account_id; cell.append(select);
    remove.append(button("移除",()=>action(async()=>{
      await ap("cohorts/save",{id:currentCohort.id,name:$("#cohortName").value,remove_mrns:[member.mrn]});
      await loadAnalysis();
    }),"quiet-button"));
    const sources=[];
    if(member.origin==="manual")sources.push("直接輸入");
    if(member.origin==="tags")sources.push("tag 群組");
    if(member.source_registrations?.length)sources.push("掛號 "+member.source_registrations.length+" 筆");
    if(member.source_records?.length)sources.push("SOAP "+member.source_records.length+" 筆");
    tr.append(el("td",member.name||"姓名待資料核對"),el("td",member.mrn),el("td",sources.join(" · ")||"門診清單"),cell,remove);
    return tr;
  }));
  for(const id of ["saveCohort","deleteCohort","startAnalysis","reloadAnalysis"]) $("#"+id).disabled=!currentCohort;
  compared.clear(); updateComparison();
}

async function saveCurrentCohort() {
  if (!currentCohort) throw new Error("請先建立分析清單。");
  const assignments = Object.fromEntries($$("select[data-mrn]",$("#cohortMembers")).map(s=>[s.dataset.mrn,s.value]));
  currentCohort=await ap("cohorts/save",{id:currentCohort.id,name:$("#cohortName").value,assignments});
  return currentCohort;
}

async function saveAnalysisAccounts() {
  await saveCurrentCohort();
  const keys=new Set(currentCohort.members.map(m=>m.account_id));
  await saveRows($$(".account-card").filter(f=>keys.has(f.dataset.id)),{credentialsOnly:true});
}

function renderAnalysisRuns() {
  const runs=analysisState.runs.filter(r=>r.cohort_id===currentCohort?.id&&JobProgress.visible(r)).slice(0,5);
  $("#analysisRuns").replaceChildren(...runs.map(run=>{
    const node=el("div",undefined,"analysis-job");
    node.dataset.transient=String(run.status==="completed");
    node.append(el("span",(statusLabels[run.status]||run.status)+" · "+run.counts.patients_done+"/"+run.counts.patients_total+" 位 · "+run.message),
      el("span","快取 "+(run.counts.analysis_cached||0)+" · 抓取 "+(run.counts.analysis_fetched||0),"muted"));
    if(active.has(run.status)) node.append(button("停止",()=>action(async()=>{await api("/api/stop",{id:run.id});await refreshAnalysisRuns();})));
    else if (["partial","interrupted","cancelled","failed"].includes(run.status)) node.append(button("續跑",()=>action(async()=>{await saveAnalysisAccounts();await ap("start",{resume:run.id});analysisRunSignature="";await refreshAnalysisRuns();})));
    node.append(JobProgress.create(run));
    if(run.issues?.length) {
      const detail=el("details"), issues=el("div"); detail.append(el("summary",run.issues.length+" 項未完成"));
      for(const i of run.issues) issues.append(el("p",i.mrn+" · "+i.stage+" · "+i.message+" "+i.code,"small"));
      detail.append(issues);node.append(detail);
    }
    return node;
  }));
  window.CataractUI?.renderPatients();
}
async function refreshAnalysisRuns() {
  const signature=JSON.stringify(history.filter(r=>r.kind==="analysis").map(r=>[r.id,r.revision,r.status]));
  if(signature===analysisRunSignature) {
    if(embeddedAccount && $("#analysisView").value==="cataract") await window.CataractUI.refreshStatus();
    return;
  }
  analysisRunSignature=signature;
  const data=await ap("cohorts",{summary:1});
  const finished=data.runs.some(r=>r.cohort_id===currentCohort?.id&&!active.has(r.status)&&
    analysisState.runs.some(old=>old.id===r.id&&active.has(old.status)));
  analysisState.runs=data.runs; renderAnalysisRuns();
  if(finished || embeddedAccount && $("#analysisView").value==="cataract")await loadAnalysisResult();
}

function rawTable(row) {
  const table=el("table",undefined,"source-table"), body=el("tbody");
  const length=Math.max(row.headers.length,row.values.length);
  for(let i=0;i<length;i++) {const tr=el("tr"), th=el("th",row.headers[i]||"欄 "+(i+1));th.scope="row";tr.append(th,el("td",row.values[i]||""));body.append(tr);}
  table.append(body);return table;
}
function compareCheckbox(row, type, exam) {
  const label=el("label",undefined,"check-label"), checkbox=el("input");
  checkbox.type="checkbox";const key=type+":"+row.id;checkbox.dataset.compare=key;checkbox.checked=compared.has(key);
  checkbox.addEventListener("change",()=>{
    if(checkbox.checked) {
      const first=[...compared.values()][0];
      if(first && (first.exam!==exam||first.type!==type)) {checkbox.checked=false;notify("請選擇同一種類的兩次檢查。",true);return;}
      if(compared.size===2) {checkbox.checked=false;notify("一次比較兩筆，請先取消其中一筆。",true);return;}
      compared.set(key,{row,type,exam});
    } else compared.delete(key);
    updateComparison();
  });
  label.append(checkbox,el("span",dayText(row.date)));return label;
}
function updateComparison() {
  $("#comparisonBar").hidden=!compared.size;
  $("#comparisonCount").textContent="已選 "+compared.size+" / 2 筆同類檢查";
  $("#showComparison").disabled=compared.size!==2;
  $$("input[data-compare]").forEach(n=>{n.checked=compared.has(n.dataset.compare);});
}
function examCard(row,type,exam,comparing=false) {
  const card=el("article",undefined,"exam-card");
  if(comparing)card.append(el("h3",dayText(row.date)+" · "+exam));else card.append(compareCheckbox(row,type,exam));
  if(type==="numeric") {
    const dl=el("dl",undefined,"exam-values");
    for(const cell of row.cells.filter(c=>c.exam===exam)) {
      const div=el("div"), label=el("dt"),value=el("dd");
      label.append(CataractNumeric.labelledText(measureLabel(cell)));
      value.append(CataractNumeric.labelledText(CataractNumeric.displayValue(cell.raw,cell.exam,cell.metric)+(cell.unit?" "+cell.unit:"")));
      div.append(label,value);dl.append(div);
    }
    card.append(dl);
    const original=el("details",undefined,"exam-original");original.open=comparing||!row.cells.length;
    original.append(el("summary","原始表格"),rawTable(row));
    if(row.header_rows?.length)original.append(el("p","來源表頭："+row.header_rows.map(parts=>parts.join(" / ")).join("；"),"small muted"));
    if(row.parsing_issues?.length)original.append(el("p","來源解析提示："+row.parsing_issues.join("、"),"small muted"));
    card.append(original);
    if(row.unparsed)card.append(el("p","部分欄位無法對應日期；保留原始資料。","small muted"));
  } else {
    card.append(el("h4",row.name));
    const labels={complete:"已保存",partial:"部分未完成",not_executed:"醫囑未執行",no_data:"尚無報告內容",no_links:"無可用報告連結"};
    card.append(el("span",labels[row.status]||row.status,"exam-status "+row.status));
    const assets=el("div",undefined,"asset-grid");
    for(const [i,asset] of (row.assets||[]).entries()) {
      const link=el("a");link.href=scopedPath("/api/analysis/asset?id="+encodeURIComponent(asset.digest));link.target="_blank";link.rel="noopener";
      if(asset.mime==="application/pdf"){link.className="pdf-link";link.textContent="開啟 PDF "+(i+1)+" ↗";}
      else {const img=el("img");img.src=link.href;img.alt=dayText(row.date)+" "+exam+" 影像 "+(i+1);img.loading="lazy";img.width=360;img.height=240;link.append(img);link.setAttribute("aria-label","開啟原始影像 "+(i+1));}
      const assetBox=el("div"),name=[row.date,row.name||exam||"醫囑附件",i+1].filter(Boolean).join(" ");
      assetBox.append(link,ClinicalUI.assetTools({url:link.href,asset,name,image:link.querySelector("img")}));assets.append(assetBox);
      const member=compareMember();
      if(member?.account_id && (!embeddedAccount || member.account_id===embeddedAccount))assetBox.append(ClinicalUI.comparisonChoice({
        account:member.account_id,mrn:member.mrn,digest:asset.digest,mime:asset.mime,
        name:row.name||exam||"醫囑附件",source:"醫囑報告",date:row.date||"",fileIndex:i+1,fileCount:row.assets.length,reportId:row.id
      }));
    }
    card.append(assets);
    if(row.texts?.length||row.details?.length)card.append(ClinicalUI.textDownload(row,[row.date,row.name||exam||"醫囑報告"].filter(Boolean).join(" ")));
    for(const text of row.texts||[]) {
      const detail=el("details",undefined,"exam-original");detail.open=comparing;
      detail.append(el("summary","報告文字"),el("pre",text.text||Object.entries(text.fields||{}).map(([k,v])=>k+": "+v).join("\n")));
      card.append(detail);
    }
  }
  card.append(el("p","保存 "+localTime(row.saved_at),"small muted"));
  return card;
}
function svgNode(name,attrs={},text) {
  const node=document.createElementNS("http://www.w3.org/2000/svg",name);
  for(const [k,v] of Object.entries(attrs))node.setAttribute(k,String(v));
  if(text!==undefined)node.textContent=text;return node;
}
function trendCharts(rows,exam) {
  const groups=new Map(), wrapper=el("div",undefined,"trends");
  for(const row of rows)for(const cell of row.cells)if(cell.exam===exam&&cell.date&&cell.value!==null) {
    if(!groups.has(cell.trend))groups.set(cell.trend,[]);groups.get(cell.trend).push(cell);
  }
  for(const points of groups.values()) {
    if(points.length<2)continue;
    points.sort((a,b)=>a.date.localeCompare(b.date));
    const figure=el("figure",undefined,"trend"), first=points[0], caption=measureLabel(first)+(first.unit?" ("+first.unit+")":"");
    figure.append(el("figcaption",caption+" · "+points.length+" 次"));
    const svg=svgNode("svg",{viewBox:"0 0 440 180",role:"img","aria-label":caption+" 時序趨勢"});
    const times=points.map(p=>Date.parse(p.date)), values=points.map(p=>p.value), minT=Math.min(...times),maxT=Math.max(...times),lo=Math.min(...values),hi=Math.max(...values);
    const coords=points.map((p,i)=>[45+(times[i]-minT)/(maxT-minT||1)*365,135-(p.value-lo)/(hi-lo||1)*110]);
    svg.append(svgNode("line",{x1:45,x2:410,y1:135,y2:135,class:"axis"}),svgNode("text",{x:3,y:30},CataractNumeric.displayValue(hi,exam,first.metric)),svgNode("text",{x:3,y:138},CataractNumeric.displayValue(lo,exam,first.metric)),
      svgNode("text",{x:45,y:165},points[0].date),svgNode("text",{x:410,y:165,"text-anchor":"end"},points.at(-1).date),
      svgNode("polyline",{points:coords.map(p=>p.join(",")).join(" ")}));
    coords.forEach((p,i)=>{const circle=svgNode("circle",{cx:p[0],cy:p[1],r:3});circle.append(svgNode("title",{},points[i].date+" · "+CataractNumeric.displayValue(points[i].raw,exam,points[i].metric)));svg.append(circle);});
    figure.append(svg);wrapper.append(figure);
  }
  return wrapper;
}
function renderExamSection(exam,rows,type) {
  const section=el("section",undefined,"exam-section"), heading=el("div",undefined,"exam-heading");
  heading.append(el("h3",exam),el("span",rows.length+" 筆 · 由舊至新"));section.append(heading);
  if(type==="numeric")section.append(trendCharts(rows,exam));
  if(!rows.length){section.append(el("p","尚無已保存資料","small muted"));return section;}
  const grid=el("div",undefined,"exam-grid");section.append(grid);
  let visible=0;const more=button("載入更多",()=>append(),"load-more");
  function append(){const end=Math.min(visible+36,rows.length);for(;visible<end;visible++)grid.append(examCard(rows[visible],type,exam));more.hidden=visible>=rows.length;}
  append();section.append(more);return section;
}
async function loadAnalysisResult({force = false, refreshStatus = true} = {}) {
  const request=++resultRequest, module=$("#analysisView").value;
  window.CataractUI?.layout();
  const member=compareMember();
  const identity=member?.account_id+":"+member?.mrn;
  if(identity!==scanPatient){window.ScanBrowser?.reset();scanPatient=identity;}
  if(member?.account_id)window.FileCompare?.context(member.account_id,member.mrn,member.name||"");
  const scanButton=$("#analysisScans");scanButton.hidden=module!=="cataract"||!embeddedAccount;
  scanButton.disabled=!member||member.account_id!==embeddedAccount;
  scanButton.title=scanButton.disabled?"請將病人抓取帳號設為目前登入帳號":"查看此病人的歷年掃描病歷";
  analysisResult=null; $("#analysisJSON").disabled=module==="surgery";
  compared.clear();updateComparison();
  $("#surgeryWorkspace").hidden=module!=="surgery";$("#analysisResults").hidden=module==="surgery";
  $("#analysisCoverage").replaceChildren();
  if(!currentCohort) {$("#analysisResults").replaceChildren(el("p","從門診清單或病歷管理選取病人","empty-result"));return;}
  if(module==="surgery"){await loadSurgery();return;}
  const values={cohort_id:currentCohort.id,mrn:$("#analysisPatient").value,module,start:$("#analysisStart").value,end:$("#analysisEnd").value};
  const cataract=module==="cataract"&&!!embeddedAccount;
  if(cataract&&!window.CataractUI.cachedResult(values))window.CataractUI.preparePatient(member);
  // Paint the selected patient's local data independently of the cohort poll.
  const reading=cataract?window.CataractUI.readResult(values,{force}):ap("results",values);
  if(cataract&&refreshStatus)void window.CataractUI.refreshStatus().catch(error=>notify(error.message,true));
  let data;
  try {data=await reading;}
  catch(error) {if(cataract&&request===resultRequest)window.CataractUI.readFailure(member,error);throw error;}
  if(request!==resultRequest)return;analysisResult=data;
  const coverage=data.coverage, context=$("#analysisCoverage");
  context.append(el("span",data.updated_at?"最後保存 "+localTime(data.updated_at):"尚未抓取此病人的檢查資料"));
  if(coverage) {
    context.append(el("div","院方就診索引 "+dayText(coverage.earliest_visit)+" ～ "+dayText(coverage.latest_visit)+" · 舊門診補查 "+coverage.backfilled_cases+" 次"));
    const detail=el("details");detail.append(el("summary","涵蓋範圍與缺漏"));
    detail.append(el("p","數值歷史自 "+coverage.numeric_history_start+"；醫囑歷史自 "+coverage.order_history_start+"。更早資料以可取得的門診補查。"));
    for(const c of coverage.unsupported_cases||[])detail.append(el("p",c.date+" · "+c.case_type+" · 無法補查此類就診"));
    for(const issue of coverage.issues||[])detail.append(el("p",issue.key+" · "+issue.message));
    if(coverage.unknown_date_cases)detail.append(el("p",coverage.unknown_date_cases+" 次就診未提供日期。"));
    context.append(detail);
  }
  if(module==="cataract"&&embeddedAccount){window.CataractUI.render(data);return;}
  const definitions=analysisState.modules[module];
  $("#analysisResults").replaceChildren(...definitions.numeric.map(exam=>renderExamSection(exam,data.numeric.filter(r=>r.exams.includes(exam)),"numeric")),
    ...definitions.orders.map(exam=>renderExamSection(exam,data.orders.filter(r=>r.exams.includes(exam)),"order")));
}

async function loadSurgery() {
  if(!currentCohort)return;
  const cohortId=currentCohort.id, data=await ap("surgery/candidates",{cohort_id:cohortId});
  if(currentCohort?.id!==cohortId)return;
  surgeryEditors.clear();$("#sheetPreview").replaceChildren();
  $("#surgeryCandidates").replaceChildren(...data.candidates.map(row=>surgeryCard(row,data.fields)));
  if(!data.candidates.length)$("#surgeryCandidates").append(el("p","此清單尚無已保存的 # Arrange SOAP；可先抓取門診 SOAP。","empty-result"));
}
function surgeryCard(row,labels) {
  const card=el("article",undefined,"surgery-candidate"), selectLabel=el("label",undefined,"check-label"), selected=el("input");
  selected.type="checkbox";selected.checked=!row.cancelled_hint;
  const invalidatePreview=()=>$("#sheetPreview").replaceChildren();
  selected.addEventListener("change",invalidatePreview);
  selectLabel.append(selected,el("span",(row.fields.name||row.fields.mrn)+" · "+row.fields.mrn));card.append(selectLabel);
  const grid=el("div",undefined,"surgery-edit-grid"), inputs={}, checks={};
  for(const [key,label] of Object.entries(labels)) {
    const wrapper=el("label",undefined,key==="plan"?"plan-field":""), line=el("span"), check=el("input");
    check.type="checkbox";check.checked=!!row.fields[key]||["mrn","date","side","procedure"].includes(key);check.disabled=key==="mrn"||key==="date";checks[key]=check;
    check.setAttribute("aria-label","更新 "+label);
    check.addEventListener("change",invalidatePreview);
    line.append(check,el("span",label));wrapper.append(line);
    let input;
    if(key==="side"||key==="ga") {input=el("select");input.append(...(key==="side"?["","OD","OS","OU"]:["","GA"]).map(v=>new Option(v||"未填",v)));}
    else {input=el("input");input.type=key==="date"?"date":"text";input.maxLength=4000;}
    input.value=row.fields[key]||"";input.dataset.field=key;
    input.setAttribute("aria-label",label+" · "+row.fields.mrn);
    if(key==="mrn"){input.readOnly=true;input.className="muted-input";}
    input.addEventListener("input",()=>{checks[key].checked=true;$("#sheetPreview").replaceChildren();});
    if(key==="side"||key==="ga"){
      const field=el("div"),valueLabel=el("label",label);
      line.setAttribute("aria-label","更新 "+label);field.append(line);valueLabel.append(input);field.append(valueLabel);
      grid.append(field);Choices.enhance(input);
    }else{wrapper.append(input);grid.append(wrapper);}
    inputs[key]=input;
  }
  card.append(grid);
  const sources=el("details");sources.append(el("summary","SOAP 來源 · "+row.sources.length+" 次紀錄"));
  for(const source of row.sources) {
    const block=el("div",undefined,"excerpt");block.append(el("strong",source.date),el("pre",source.excerpt),
      button("查看完整 SOAP",()=>action(()=>openSavedRecord(source.record_id))));sources.append(block);
  }
  for(const [key,values] of Object.entries(row.differences||{}))sources.append(el("p",(labels[key]||key)+" 歷次值："+values.join(" → ")));
  for(const [key,values] of Object.entries(row.ambiguities||{}))sources.append(el("p",(labels[key]||key)+" 待確認："+values.join(" / ")));
  card.append(sources);
  if(Object.keys(row.differences||{}).length||Object.keys(row.ambiguities||{}).length)
    card.append(el("p","來源有差異的欄位已留白，請核對後填入。","small preview-conflict"));
  if(row.patient_info && Object.keys(row.patient_info).length) {
    const info=el("details");
    info.append(el("summary","院內病人資料 · "+localTime(row.patient_info_at)),el("pre",JSON.stringify(row.patient_info,null,2)));
    card.append(info);
  }
  const schedules=el("details");schedules.open=row.date_conflict;
  schedules.append(el("summary","院內排程 · "+row.schedules.length+" 筆"));
  const reviewed=el("input");reviewed.type="checkbox";
  reviewed.addEventListener("change",invalidatePreview);
  for(const schedule of row.schedules) {
    const node=el("div",undefined,"schedule-option");
    node.append(el("span",[schedule.date,schedule.start_time,schedule.procedure,schedule.side,schedule.room,schedule.status].filter(Boolean).join(" · ")),
      button("採用此刀日",()=>{inputs.date.value=schedule.date;reviewed.checked=true;$("#sheetPreview").replaceChildren();}));
    schedules.append(node);
  }
  card.append(schedules);
  if(row.date_conflict||row.cancelled_hint) {
    const label=el("label",undefined,"check-label reviewed");
    label.append(reviewed,el("span",(row.cancelled_hint?"含取消文字；":"")+(row.date_conflict?"日期有差異；":"")+"已核對來源與刀日"));card.append(label);
  }
  const targetLabel=el("label","刀表對應", "exam-original"), target=el("select");
  target.append(new Option("自動核對唯一匹配",""));targetLabel.append(target);card.append(targetLabel);
  target.addEventListener("change",invalidatePreview);
  surgeryEditors.set(row.id,{row,card,selected,inputs,checks,reviewed,target});
  return card;
}
function collectProposals() {
  return [...surgeryEditors.values()].filter(e=>e.selected.checked).map(e=>{
    const fields=Object.fromEntries(Object.entries(e.inputs).map(([k,n])=>[k,n.value]));
    const selected=Object.entries(e.checks).filter(([,n])=>n.checked).map(([k])=>k);
    return {id:e.row.id,fields,selected_fields:selected,clear_fields:selected.filter(k=>!fields[k]&&!!e.row.fields[k]),
      reviewed:e.reviewed.checked,target:e.target.value==="new"?"":e.target.value,new_event:e.target.value==="new"};
  });
}
async function createSheetPreview() {
  const btn=$("#previewSheet");btn.disabled=true;
  try {
    const value=await ap("surgery/preview",{cohort_id:currentCohort.id,proposals:collectProposals()});
    $("#sheetPreview").replaceChildren(previewNode(value));
    $("#sheetPreview").scrollIntoView({block:"start"});
  } finally {btn.disabled=false;}
}
function previewNode(value) {
  const container=el("section",undefined,"sheet-preview");
  container.append(el("h3","刀表差異預覽"),el("p",value.message||"核對變更後，再按「套用更新」。","small muted"));
  for(const structure of value.structures||[])container.append(el("div",structure,"sheet-structure"));
  for(const item of value.items) {
    if(item.status==="conflict") {
      const box=el("div",undefined,"preview-conflict");box.append(el("strong",item.fields.name+" · "+item.reason));
      const editor=surgeryEditors.get(item.id);
      if(editor){
        editor.target.replaceChildren(new Option("請選擇對應手術",""),
          ...item.options.map(o=>new Option(o.ref+" · "+o.fields.date+" "+o.fields.side+" "+o.fields.procedure,o.ref)),
          new Option("確認為另一次手術，新增","new"));
        box.append(button("回到此病人選擇對應",()=>editor.card.scrollIntoView({block:"center"})));
      }
      container.append(box);continue;
    }
    const details=el("details");details.open=true;
    const names={insert:"新增",move:"改期搬移",update:"更新"};
    details.append(el("summary",(names[item.action]||"更新")+" · "+item.fields.name+" · "+item.fields.mrn+" · "+item.position));
    if(!item.changes.length)details.append(el("p","欄位內容相同","small muted"));
    else {
      const table=el("table",undefined,"source-table"), head=el("thead"), hrow=el("tr"), body=el("tbody");
      for(const text of ["欄位／位置","原內容","更新內容"])hrow.append(el("th",text));head.append(hrow);
      for(const change of item.changes){const tr=el("tr");tr.append(el("td",change.label+" "+(change.cell||"")),el("td",change.before||"（空白）","before-value"),el("td",change.after||"（清空）","after-value"));body.append(tr);}
      table.append(head,body);details.append(table);
    }
    container.append(details);
  }
  const actions=el("div",undefined,"actions");
  const applyButton=button("套用更新",()=>action(async()=>{
    applyButton.disabled=true;
    try {const result=await ap("surgery/apply",{id:value.id});container.replaceWith(previewNode(result));}
    finally {applyButton.disabled=false;}
  }),"primary");
  applyButton.disabled=!value.can_apply||["applied","applied_changed"].includes(value.status);
  const checkButton=button("核對更新結果",()=>action(async()=>{
    checkButton.disabled=true;try {const result=await ap("surgery/check",{id:value.id});container.replaceWith(previewNode(result));}
    finally {checkButton.disabled=false;}
  }));
  actions.append(applyButton,checkButton);
  const link=el("a","開啟 Google 刀表 ↗");link.href="https://docs.google.com/spreadsheets/d/"+encodeURIComponent(value.spreadsheet_id)+"/edit";link.target="_blank";link.rel="noopener";actions.append(link);
  container.append(actions);return container;
}

$$("[data-analysis-close]").forEach(n=>n.addEventListener("click",()=>$("#"+n.dataset.analysisClose).close()));
$("#createCohortForm").addEventListener("submit",async event=>{
  event.preventDefault();const control=$('button[type="submit"]',event.currentTarget);control.disabled=true;
  $("#cohortError").hidden=true;
  try {
    let values={...cohortSeed,name:$("#newCohortName").value};
    if(cohortSeed.source==="tags") {
      const key=$("#manualAccount").value,form=$$(".account-card").find(f=>f.dataset.id===key);
      if(!form)throw new Error("請先設定登入帳號。");
      await saveRows([form],{credentialsOnly:true});
      values={...values,account_id:key};
    }
    const saved=await ap("cohorts/save",values);
    $("#cohortDialog").close();$("#accountsDetails").open=false;
    $("#analysisView").value=$('input[name="analysisModule"]:checked')?.value||"retina";
    showPane("analysis");await loadAnalysis(saved.id);
    $("#analysisTitle").scrollIntoView({block:"start"});
  } catch(error) {$("#cohortError").textContent=error.message;$("#cohortError").hidden=false;}
  finally {control.disabled=false;}
});
$("#cohortSelect").addEventListener("change",()=>action(async()=>{selectCohort();renderAnalysisRuns();await loadAnalysisResult();}));
$("#saveCohort").addEventListener("click",()=>action(async()=>{await saveCurrentCohort();notify("分析清單與帳號已保存");}));
$("#deleteCohort").addEventListener("click",()=>action(async()=>{
  if(currentCohort&&await confirmDelete("移除此分析清單？已抓取的檢查資料保留。")){await ap("cohorts/delete",{id:currentCohort.id});await loadAnalysis();}
}));
$("#startAnalysis").addEventListener("click",()=>action(async()=>{
  const control=$("#startAnalysis");control.disabled=true;
  const pending=JobProgress.pending("#analysisRuns","歷年檢查分析","正在準備抓取帳號與任務…");
  try {
    await saveAnalysisAccounts();
    const modules=$$('input[name="analysisModule"]:checked').map(n=>n.value), mode=$("#analysisRefresh").value;
    await ap("start",{cohort_id:currentCohort.id,modules,start:$("#analysisStart").value,end:$("#analysisEnd").value,refresh:mode!=="cache",force:mode==="force"});
    if(embeddedAccount)parent.postMessage({type:"bot:task-started"},location.origin);
    if(modules.length===1)$("#analysisView").value=modules[0];
    await refresh();await loadAnalysisResult();
  } finally {pending?.remove();control.disabled=false;}
}));
for(const id of ["analysisPatient","analysisView"])$("#"+id).addEventListener("change",()=>action(()=>loadAnalysisResult()));
$("#analysisScans").addEventListener("click",()=>action(async()=>{
  const member=compareMember();
  if(!member||!currentCohort||member.account_id!==embeddedAccount)throw new Error("請先選擇由目前帳號抓取的病人。");
  await window.ScanBrowser.open({account:embeddedAccount,mrn:member.mrn,patient_name:member.name,cohort_id:currentCohort.id,resource:"scans",
    request:(path,body)=>api("/api"+path,body),allowFetch:()=>!config?.read_only});
}));
$("#reloadAnalysis").addEventListener("click",()=>action(()=>loadAnalysisResult()));
$("#reloadSurgery").addEventListener("click",()=>action(()=>loadSurgery()));
$("#previewSheet").addEventListener("click",()=>action(()=>createSheetPreview()));
$("#clearComparison").addEventListener("click",()=>{compared.clear();updateComparison();});
$("#showComparison").addEventListener("click",()=>{
  const values=[...compared.values()].sort((a,b)=>a.row.date.localeCompare(b.row.date));if(values.length!==2)return;
  $("#comparisonTitle").textContent=values[0].exam+" · 歷次比較";
  $("#comparisonContent").replaceChildren(...values.map(v=>examCard(v.row,v.type,v.exam,true)));$("#comparisonDialog").showModal();
});
$("#analysisJSON").addEventListener("click",()=>{if(analysisResult)download("VGHKS-analysis-"+analysisResult.member.mrn+".json",JSON.stringify(analysisResult,null,2),"application/json;charset=utf-8");});
async function showGoogleSettings() {
  const value=await ap("google");$("#googleSheetId").value=value.spreadsheet_id;
  $("#googleStatus").textContent=googleCredentialStatus(value);
  $("#googleKeyFile").value="";$("#googleConnectionResult").textContent="";$("#googleDialog").showModal();
}
function googleCredentialStatus(value) {
  return value.configured?"已保存可攜式金鑰 · "+value.email:"尚未匯入服務帳戶金鑰";
}
$("#googleSettingsButton").addEventListener("click",()=>action(()=>showGoogleSettings()));
async function saveGoogleSettings(test=false) {
  if(!$("#googleForm").reportValidity())return;
  const controls=$$('button[type="submit"],#testGoogleConnection,#removeGoogleKey',$("#googleForm"));
  controls.forEach(n=>n.disabled=true);$("#googleConnectionResult").textContent=test?"正在測試讀取連線…":"正在保存…";
  try {
    const file=$("#googleKeyFile").files[0], values={spreadsheet_id:$("#googleSheetId").value};
    if(file){if(file.size>65536)throw new Error("金鑰檔案過大。");values.key=JSON.parse(await file.text());}
    const result=await ap("google/save",values);$("#googleKeyFile").value="";
    $("#googleStatus").textContent=googleCredentialStatus(result);
    $("#googleConnectionResult").textContent="連線設定已保存。";
    if(test) {
      const value=await ap("google/test");
      $("#googleConnectionResult").textContent=value.title+" · "+value.months.length+" 個月份分頁。"+value.message;
    }
  } catch(error) {$("#googleConnectionResult").textContent=error.message;}
  finally {controls.forEach(n=>n.disabled=false);}
}
$("#googleForm").addEventListener("submit",event=>{event.preventDefault();saveGoogleSettings();});
$("#testGoogleConnection").addEventListener("click",()=>saveGoogleSettings(true));
$("#removeGoogleKey").addEventListener("click",()=>action(async()=>{
  if(await confirmDelete("移除此帳號資料庫保存的 Google 金鑰？")){await ap("google/remove");$("#googleStatus").textContent="已移除金鑰";$("#googleConnectionResult").textContent="";}
}));
$("#sheetHistoryButton").addEventListener("click",()=>action(async()=>{
  const data=await ap("surgery/history");$("#sheetHistoryContent").replaceChildren(...data.previews.map(previewNode));
  if(!data.previews.length)$("#sheetHistoryContent").append(el("p","尚無刀表更新紀錄","muted"));
  $("#sheetHistoryDialog").showModal();
}));
