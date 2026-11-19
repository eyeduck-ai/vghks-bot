"use strict";

// The embedded cataract view keeps one patient's cached workbench in place.
window.CataractUI = (() => {
  let status = null, orderSequence = 0, patientsCollapsed = false, patientsScroll = 0;
  let currentKey = "", currentState = null, currentUI = null;
  let imageResizeObserver = null;
  let patientFitFrame = null, patientResize = null;
  const patientStates = new Map();
  const resultCache = new Map(), resultReads = new Map();
  let cacheContext = "", statusRead = null, statusAt = 0;
  const contextKey = () => [embeddedAccount, currentCohort?.id || ""].join(":");
  const resultKey = values => [embeddedAccount, values.cohort_id, values.mrn].join(":");
  const resultRange = values => JSON.stringify([values.start || "", values.end || ""]);

  function releasePreviews() {
    currentState?.orderBiometry?.dismiss();
    currentUI?.orderExtraction?.dispose();
    currentUI?.scans?.clear();
  }

  function mountBiometryWorkspace(workspace) {
    const layout = $("#cataractLayout"), area = $("#analysisResults");
    const x = window.scrollX, y = window.scrollY;
    area.append(workspace);
    layout.classList.add("cataract-batch-open");
    window.scrollTo?.(0, 0);
    return () => {
      workspace.remove();
      layout.classList.remove("cataract-batch-open");
      currentUI?.orderExtraction?.refreshReview();
      currentUI?.scans?.refreshBiometryReview();
      window.scrollTo?.(x, y);
    };
  }

  function cachedResult(values) {
    const cached = resultCache.get(resultKey(values));
    return cached?.range === resultRange(values) ? cached.data : null;
  }

  function readResult(values, {force = false} = {}) {
    const key = resultKey(values), range = resultRange(values), context = cacheContext;
    const cached = !force && cachedResult(values);
    if (cached) return Promise.resolve(cached);
    const previous = resultReads.get(key);
    if (previous?.range === range) return previous.promise;
    const read = {range};
    read.promise = ap("results", values).then(data => {
      if (cacheContext === context && resultReads.get(key) === read)
        resultCache.set(key, {range, data});
      return data;
    }).finally(() => {if (resultReads.get(key) === read) resultReads.delete(key);});
    resultReads.set(key, read);
    return read.promise;
  }

  function preparePatient(member) {
    if (!member || currentKey === resultKey({cohort_id:currentCohort.id, mrn:member.mrn}) && currentUI) return;
    stateFor(member);
    restoreHeaderTools();
    coverage.replaceChildren();
    const heading = patientIdentity(member);
    const waiting = el("p", "正在讀取已存 SOAP 與數值…", "small muted");
    waiting.setAttribute("role", "status");
    $("#analysisResults").replaceChildren(heading, waiting);
  }

  function patientIdentity(member) {
    const heading = el("h2", (window.ClinicalUI?.patientName?.(member.name) ?? member.name) || "姓名未提供", "cataract-identity");
    heading.append(ClinicalUI.mrnBadge(member.mrn, message => notify(message)), ClinicalUI.ageBadge(member));
    return heading;
  }

  function readFailure(member, error) {
    if (!member || currentUI || $("#analysisPatient").value !== member.mrn) return;
    const area = $("#analysisResults"), waiting = area.querySelector('[role="status"]');
    if (waiting) {waiting.textContent = error.message;waiting.className = "form-error";}
    area.append(button("重讀已存資料", () => action(() => loadAnalysisResult({force:true, refreshStatus:false}))));
  }
  const toolbar = $("#analysisPane .analysis-view-toolbar"), coverage = $("#analysisCoverage");
  const toolbarAnchor = document.createComment("cataract toolbar home");
  const coverageAnchor = document.createComment("cataract coverage home");
  toolbar.before(toolbarAnchor);
  coverage.before(coverageAnchor);
  function restoreHeaderTools() {
    if (toolbar.previousSibling !== toolbarAnchor) toolbarAnchor.after(toolbar);
    if (coverage.previousSibling !== coverageAnchor) coverageAnchor.after(coverage);
  }
  let expandedStates = new Map();
  try {
    if (parent !== window && parent.location.origin === location.origin) {
      parent.CataractExpanded ||= new Map();
      expandedStates = parent.CataractExpanded;
    }
  } catch (_) { /* The tool can also run outside the same-origin workbench. */ }
  const activeView = () => !!embeddedAccount && $("#analysisView").value === "cataract";
  const patientView = () => !!embeddedAccount && ["retina", "cataract", "surgery"].includes($("#analysisView").value);
  function fitPatientRail() {
    const list = $("#cataractPatients");
    if (!patientView() || list.hidden || typeof list.getBoundingClientRect !== "function" || patientFitFrame !== null) return;
    const fit = () => {
      patientFitFrame = null;
      if (list.hidden || !patientView()) return;
      const top = Math.max(8, list.getBoundingClientRect().top), height = Math.max(120, window.innerHeight - top - 12) + "px";
      if (list.style.getPropertyValue("--patient-rail-height") !== height) list.style.setProperty("--patient-rail-height", height);
    };
    if (typeof requestAnimationFrame === "function") patientFitFrame = requestAnimationFrame(fit);else fit();
  }
  window.addEventListener("resize", fitPatientRail, {passive:true});
  window.addEventListener("resize", refitImagePreviews, {passive:true});
  window.addEventListener("scroll", fitPatientRail, {passive:true});

  function stateFor(member) {
    const key = [embeddedAccount, currentCohort?.id, member.mrn].join(":");
    const expansionKey = [embeddedAccount, member.mrn].join(":");
    if (!patientStates.has(key)) patientStates.set(key, {
      view: "overview", overviewMobile: "soap", mobile: "orders", numericOpen: true, eye: "both", expansionKey,
      expanded: new Set(expandedStates.get(expansionKey) || window.CataractNumeric.exams),
      orderInitialized: false, orderExpanded: new Set(),
      selectedOrderId: "", selectedAssetDigest: "", orderRow: null, report: null, reportSavedAt: "", imageViews: new Map()
    });
    if (key !== currentKey) {
      currentKey = key;
      orderSequence++;
      releasePreviews();
      currentUI = null;
    }
    currentState = patientStates.get(key);
    return currentState;
  }

  function layout() {
    const enabled = patientView(), cataract = activeView();
    if (cataract && cacheContext !== contextKey()) {
      cacheContext = contextKey();
      resultCache.clear();resultReads.clear();patientStates.clear();
      status = null;statusRead = null;statusAt = 0;
      releasePreviews();
      currentKey = "";currentState = null;currentUI = null;orderSequence++;
    }
    if (!cataract || !currentCohort) {
      currentState?.orderBiometry?.dismiss();
      currentUI?.scans?.dismissBiometry?.();
      restoreHeaderTools();
    }
    const area = $("#cataractLayout"), list = $("#cataractPatients");
    if (!patientResize && typeof ResizeObserver === "function") {
      patientResize = new ResizeObserver(fitPatientRail);patientResize.observe($("#analysisPane"));
    }
    area.classList.toggle("active", enabled);
    area.classList.toggle("patients-collapsed", patientsCollapsed);
    area.dataset.module = $("#analysisView").value;
    list.setAttribute("aria-label", "病人清單");
    list.hidden = !enabled || patientsCollapsed;
    $("#analysisPatient").closest("label").hidden = enabled;
    $("#cataractUpdate").hidden = !cataract;
    let exportButton = $("#cataractExport");
    if (!exportButton) {exportButton = button("匯出報告 ZIP", () => window.CataractExport.open());exportButton.id = "cataractExport";$("#cataractUpdate").after(exportButton);}
    exportButton.hidden = !cataract;
    $("#reloadAnalysis").hidden = cataract;
    let toggle = $("#cataractPatientsToggle");
    if (enabled && !toggle) {
      toggle = button("", () => {if(!patientsCollapsed)patientsScroll=list.scrollTop;patientsCollapsed = !patientsCollapsed;layout();if(!patientsCollapsed)list.scrollTop=patientsScroll;}, "cataract-patients-toggle patient-rail-toggle");
      const arrow = el("span", undefined, "sidebar-arrow");arrow.setAttribute("aria-hidden", "true");toggle.append(arrow);
      toggle.id = "cataractPatientsToggle";
      toggle.setAttribute("aria-controls", "cataractPatients");
      list.after(toggle);
    }
    if (toggle) {
      toggle.hidden = !enabled;
      toggle.setAttribute("aria-expanded", String(!patientsCollapsed));
      toggle.setAttribute("aria-label", patientsCollapsed ? "展開病人清單" : "收合病人清單");
      toggle.title = toggle.getAttribute("aria-label");
    }
    if (enabled) renderPatients();
    refitImagePreviews();
  }

  async function refreshStatus({force = false} = {}) {
    layout();
    if (!activeView() || !currentCohort) {status = null; renderPatients(); return null;}
    if (statusRead) return statusRead;
    if (!force && status && performance.now() - statusAt < 1000) return status;
    const context = cacheContext, cohort = currentCohort.id;
    const read = ap("cataract/status", {cohort_id: cohort}).then(async value => {
      if (context !== cacheContext || cohort !== currentCohort?.id || !activeView()) return null;
      status = value;statusAt = performance.now();
      let changed = false;
      for (const row of value.members || []) {
        const key = resultKey({cohort_id:cohort, mrn:row.mrn}), cached = resultCache.get(key);
        if (cached && row.data_revision && cached.data.data_revision !== row.data_revision) {
          resultCache.delete(key);
          if ($("#analysisPatient").value === row.mrn) changed = true;
        }
      }
      renderPatients();syncAttachments();
      if (changed) await loadAnalysisResult({refreshStatus:false});
      return value;
    }).finally(() => {if (statusRead === read) statusRead = null;});
    statusRead = read;
    return read;
  }

  async function enqueue(action = "start") {
    const member = currentCohort?.members.find(row => row.mrn === $("#analysisPatient").value);
    if (!member || config?.read_only || member.account_id !== embeddedAccount) return;
    const context = cacheContext;
    if (!status) await refreshStatus();
    if (context !== cacheContext || $("#analysisPatient").value !== member.mrn) return;
    const current = status?.members?.find(row => row.mrn === member.mrn), queue = status?.queue;
    if (action === "prioritize" && (!current || current.ready || current.cache_cleared || current.active_run_id ||
        queue?.state === "paused" || (current.attempted && !queue?.pending?.includes(member.mrn)))) return;
    if (action === "start" && ((queue && !["idle", "completed"].includes(queue.state)) ||
        !status?.members?.some(row => !row.ready && !row.attempted && !row.cache_cleared))) return;
    if (action === "selected" && (!current || current.ready || current.cache_cleared || current.active_run_id ||
        queue?.state === "paused" || current.attempted && !queue?.pending?.includes(member.mrn))) return;
    const pending = JobProgress.pending("#analysisRuns", "白內障術前分析", "正在安排背景抓取…");
    try {
      await ap("cataract/queue", {cohort_id:currentCohort.id, action, mrn:member.mrn});
      parent.postMessage({type:"bot:task-started"}, location.origin);
      await refreshStatus({force:true});
    } finally {pending?.remove();}
  }

  function renderPatients() {
    const list = $("#cataractPatients");
    const selected = currentCohort?.members.find(member => member.mrn === $("#analysisPatient").value), identity = $("#analysisSelectedPatient");
    identity.hidden = !patientView() || activeView();
    identity.textContent = selected ? ($("#analysisView").value === "surgery" ? "定位：" : "") + [(window.ClinicalUI?.patientName?.(selected.name) ?? selected.name) || "姓名未提供", selected.mrn].join(" · ") : "";
    if (list.hidden) return;
    const previousScroll = list.scrollTop;
    const focused = list.contains(document.activeElement) ? document.activeElement.dataset : null;
    const focusKey = focused?.queueAction ? "queue" : focused?.patientMrn;
    list.replaceChildren();
    if (!patientView() || !currentCohort) return;
    const heading = el("h3", `病人清單 · ${currentCohort.members.length} 位`);
    list.append(heading);
    const cataract = activeView(), queue = cataract ? status?.queue : null;
    const states = new Map((status?.members || []).map(row => [row.mrn, row]));
    if (queue && !["idle", "completed"].includes(queue.state)) {
      const controls = el("div", undefined, "cataract-queue-controls");
      const paused = queue.state === "paused";
      controls.dataset.state = queue.state;
      const members = currentCohort.members.filter(member => member.account_id === embeddedAccount);
      const saved = members.filter(member => states.get(member.mrn)?.ready).length;
      const summary = el("div", undefined, "cataract-queue-summary"), caption = el("div", undefined, "cataract-queue-caption");
      caption.append(el("strong", "集合進度"), el("span", `${saved} / ${members.length} 位已保存`));
      const bar = el("progress", undefined, "cataract-queue-progress");
      bar.max = Math.max(1, members.length);bar.value = saved;
      bar.setAttribute("aria-label", "本集合已保存病人");
      bar.setAttribute("aria-valuetext", `${saved} / ${members.length} 位已保存`);
      const current = members.find(member => member.mrn === queue.active_mrn);
      const detail = paused ? "已暫停" : current ? `正在處理 ${(window.ClinicalUI?.patientName?.(current.name) ?? current.name) || current.mrn}` : "等待下一位";
      const state = el("small", detail);state.title = detail;
      summary.append(caption, bar, state);controls.append(summary);
      const toggle = button("", () => action(() => enqueue(paused ? "resume" : "pause")), "quiet-button");
      const icon = el("span", undefined, paused ? "cataract-queue-icon is-play" : "cataract-queue-icon is-pause");
      icon.setAttribute("aria-hidden", "true");toggle.append(icon);
      toggle.setAttribute("aria-label", paused ? "繼續背景抓取" : "暫停背景抓取");
      toggle.title = toggle.getAttribute("aria-label");
      toggle.dataset.queueAction = "toggle";
      toggle.disabled = !!config?.read_only;
      controls.title = queue.reason || "";
      controls.append(toggle);list.append(controls);
    }
    for (const member of currentCohort.members) {
      const state = states.get(member.mrn);
      const label = state?.active_run_id ? "抓取中" : state?.ready ? "已保存" :
        queue?.pending?.includes(member.mrn) ? "等待中" :
        state?.cache_cleared ? "本機資料已清除" : state?.attempted ? "部分待續跑" : "尚未抓取";
      const item = button("", () => action(async () => {
        if ($("#analysisView").value === "surgery") {
          $("#analysisPatient").value = member.mrn;
          renderPatients();focusSurgeryPatient(member.mrn);return;
        }
        if ($("#analysisPatient").value === member.mrn) return;
        $("#analysisPatient").value = member.mrn;
        const displayed = loadAnalysisResult();
        if (activeView()) void enqueue("prioritize").catch(error => notify(error.message, true));
        await displayed;
        if ($("#analysisPatient").value === member.mrn) $("#analysisResults").scrollTop = 0;
      }), "cataract-patient");
      item.setAttribute("aria-current", String($("#analysisPatient").value === member.mrn));
      item.dataset.patientMrn = member.mrn;
      const name = (window.ClinicalUI?.patientName?.(member.name) ?? member.name) || "姓名未提供";
      item.title = [name, member.mrn, cataract ? label : ""].filter(Boolean).join(" · ");
      item.setAttribute("aria-label", item.title);
      const identity = el("strong", name);identity.title = name;
      item.append(identity, el("span", member.mrn, "small muted cataract-patient-id"));
      if (cataract) item.append(el("span", label, "cataract-patient-status"));
      list.append(item);
    }
    list.scrollTop = previousScroll;
    if (focusKey) {
      const target = [...list.querySelectorAll("button")].find(item =>
        focusKey === "queue" ? item.dataset.queueAction : item.dataset.patientMrn === focusKey);
      target?.focus({preventScroll:true});
    }
    fitPatientRail();
  }

  async function start({refresh: update = false, automatic = false, resume = false} = {}) {
    const member = currentCohort?.members.find(row => row.mrn === $("#analysisPatient").value);
    if (!member || config?.read_only || member.account_id !== embeddedAccount) return;
    const current = (status || await refreshStatus())?.members?.find(row => row.mrn === member.mrn);
    if (!current || current.active_run_id) return;
    if (automatic && (current.ready || current.attempted)) return;
    if (!automatic && !update && current.ready) return;
    const pending = JobProgress.pending("#analysisRuns", "白內障術前分析", "正在建立此病人的抓取任務…");
    try {
      await saveAnalysisAccounts();
      const request = resume && current.resume_run_id ? {resume: current.resume_run_id} :
        {cohort_id: currentCohort.id, modules: ["cataract"], mrn: member.mrn,
          start: "", end: "", refresh: update, force: false};
      await ap("start", request);
      parent.postMessage({type: "bot:task-started"}, location.origin);
      await refresh();
      await refreshStatus();
      await loadAnalysisResult();
    } finally {pending?.remove();}
  }

  function numericPanel(data, state) {
    return window.CataractNumeric.create(data.numeric || [], state, () =>
      expandedStates.set(state.expansionKey, new Set(state.expanded)));
  }

  function soapSection(data) {
    const section = el("section", undefined, "cataract-section cataract-soap soap-record");
    const record = data.latest_soap?.record;
    section.append(el("h3", "最新眼科門診 SOAP"));
    if (!record) {
      const message = data.latest_soap?.status === "no_visit" ? "沒有可核對的眼科門診就診。" :
        data.latest_soap?.status === "missing" ? "眼科門診就診沒有可讀的 SOAP。" :
        "尚未取得 SOAP；已保存的其他資料仍可檢閱。";
      section.append(el("p", message, "small muted"));
    } else {
      section.append(el("p", [record.date, record.section, record.case_no].filter(Boolean).join(" · "), "caption"));
      section.append(SOAPView.create(record));
    }
    return section;
  }

  function orderPanel(data, state, section = el("section", undefined, "cataract-section cataract-orders")) {
    if (!section.querySelector("h3")) section.append(el("h3", "歷年醫囑"));
    const account = embeddedAccount, cohortId = currentCohort.id, mrn = data.member.mrn;
    const database = config?.context;
    const canExtract = data.member.account_id === account && config?.biometry_variant !== "basic";
    if (state.orderBiometry && (!canExtract || state.orderBiometry.database !== database)) {
      if (currentUI?.orders === section) clearOrderExtraction();
      state.orderBiometry.clear();
      state.orderBiometry.element.remove();
      state.orderBiometry = null;
    }
    if (!state.orderBiometry && canExtract && window.CataractBiometry) {
      state.orderBiometry = window.CataractBiometry.create({account, cohortId, mrn, source: "orders",
        request: values => ap("cataract/biometry", values),
        mountWorkspace: mountBiometryWorkspace,
        textOnly: config?.biometry_variant === "lite",
        current: () => activeView() && currentState === state && database === config?.context &&
          currentCohort?.id === cohortId && embeddedAccount === account && $("#analysisPatient").value === mrn &&
          currentCohort.members.some(member => member.mrn === mrn && member.account_id === account),
        readonly: !!config?.read_only,
        range: () => ({start: $("#analysisStart").value, end: $("#analysisEnd").value})});
      state.orderBiometry.database = database;
    }
    if (state.orderBiometry) {
      if (state.orderBiometry.element.parentElement !== section)
        section.querySelector("h3").after(state.orderBiometry.element);
      const pdfs = (data.orders || []).flatMap(row => (row.attachments || [])
        .filter(asset => asset.mime === "application/pdf" && asset.available !== false)
        .map(asset => ({id: row.id, digest: asset.digest, date: row.date,
          label: [row.name, asset.name].filter(Boolean).join(" · ")})));
      state.orderBiometry.setDocuments(pdfs);
    }
    section.querySelector(".empty-orders")?.remove();
    const preferred = analysisState.modules.cataract.orders, groups = new Map();
    for (const row of data.orders || []) {
      const name = row.name?.trim() || "名稱未提供";
      if (!groups.has(name)) groups.set(name, []);
      if (!groups.get(name).some(item => item.id === row.id)) groups.get(name).push(row);
    }
    const rank = name => {const index = preferred.findIndex(value => (groups.get(name)[0].exams || []).includes(value));return index < 0 ? preferred.length : index;};
    const names = [...groups.keys()].sort((a,b) => rank(a)-rank(b)||a.localeCompare(b));
    state.orderKnown ||= new Set();
    for (const group of [...section.querySelectorAll(".cataract-order-group")]) if (!groups.has(group.dataset.name)) group.remove();
    for (const [position,name] of names.entries()) {
      if (!state.orderKnown.has(name)) {state.orderExpanded.add(name);state.orderKnown.add(name);}
      let group = [...section.querySelectorAll(".cataract-order-group")].find(node => node.dataset.name === name);
      if (!group) {
        group=el("details",undefined,"cataract-order-group");group.dataset.name=name;
        group.append(el("summary"));group.open=state.orderExpanded.has(name);
        group.addEventListener("toggle",()=>group.open?state.orderExpanded.add(name):state.orderExpanded.delete(name));
      }
      const rows=groups.get(name).sort((a,b)=>(b.date||"").localeCompare(a.date||""));
      group.querySelector("summary").textContent=`${name} · ${rows.length} 筆`;
      for (const item of [...group.querySelectorAll(".cataract-order-item")]) if (!rows.some(row=>row.id===item.dataset.orderId)) item.remove();
      for (const [index,row] of rows.entries()) {
        let item=[...group.querySelectorAll(".cataract-order-item")].find(node=>node.dataset.orderId===row.id);
        if(!item){
          item=el("div",undefined,"cataract-order-item");item.dataset.orderId=row.id;
          const line=el("div",undefined,"cataract-order-line");
          const select=button("",()=>action(()=>openOrder(item._order)),"cataract-order-row");
          select.dataset.orderId=row.id;select.append(el("strong"),el("span",undefined,"small muted"));line.append(select);
          if(!config?.read_only){
            line.classList.add("has-refresh");
            const refresh=button("↻",()=>action(()=>refreshOrder(item._order)),"cataract-order-refresh quiet-button");
            refresh.setAttribute("aria-label",`更新 ${row.name} ${row.date||"日期未提供"} 的報告`);refresh.title=refresh.getAttribute("aria-label");line.append(refresh);
          }
          item.append(line);
        }
        item._order=row;
        const select=item.querySelector(".cataract-order-row");select.title=[row.name,row.date,row.case_no].filter(Boolean).join(" · ");
        select.querySelector("strong").textContent=row.date||"日期未提供";
        const count=row.report_loaded?`${row.asset_count} 份檔案`:"尚未抓取";
        select.querySelector("span").textContent=row.report_loaded&&!row.report_complete?`部分完成 · ${count}`:count;
        if(group.children[index+1]!==item)group.insertBefore(item,group.children[index+1]||null);
      }
      const groupPosition = position + (state.orderBiometry ? 2 : 1);
      if(section.children[groupPosition]!==group)section.insertBefore(group,section.children[groupPosition]||null);
    }
    if(!names.length)section.append(el("p","尚無符合的醫囑。","small muted empty-orders"));
    return section;
  }

  function syncOrderControls() {
    if(!currentUI)return;
    for(const item of currentUI.orders.querySelectorAll(".cataract-order-item")){
      const row=item._order,selected=row.id===currentState.selectedOrderId;
      item.querySelector(".cataract-order-row").setAttribute("aria-current",String(selected));
      const assets=row.attachments||[];
      let choices=item.querySelector(".cataract-file-choices");
      if(!choices){choices=el("div",undefined,"cataract-file-choices");choices.setAttribute("aria-label",`${row.name} 的附件`);item.append(choices);}
      for(const file of [...choices.children])if(!assets.some(asset=>asset.digest===file.dataset.digest))file.remove();
      for(const [index,asset] of assets.entries()){
        let file=[...choices.children].find(node=>node.dataset.digest===asset.digest);
        if(!file){
          file=el("div",undefined,"cataract-file-choice");file.dataset.digest=asset.digest;
          const check=el("input");check.type="checkbox";check.dataset.compareDigest=asset.digest;
          check.setAttribute("aria-label",`將 ${row.name} ${asset.name||'檔案 '+(index+1)} 加入比較`);
          check.addEventListener("change",()=>{
            try{
              const current=item._order,files=current.attachments||[],fileIndex=files.findIndex(file=>file.digest===asset.digest)+1;
              if(check.checked)window.FileCompare.add({account:embeddedAccount,mrn:$("#analysisPatient").value,digest:asset.digest,mime:asset.mime,name:[current.name,asset.name].filter(Boolean).join(" · "),source:"醫囑報告",date:current.date||"",fileIndex,fileCount:files.length,reportId:current.id});
              else window.FileCompare.remove(asset.digest);
            }catch(error){notify(error.message,true);}
            syncAttachments();
          });
          const choose=button(asset.name||`檔案 ${index+1}`,()=>action(()=>openOrder(item._order,asset.digest)),"quiet-button");
          choose.dataset.assetDigest=asset.digest;choose.title=`${row.name} ${row.date||""} · ${asset.name||'檔案 '+(index+1)}`;
          const name=[row.date,row.name,index+1].filter(Boolean).join(" ");
          file.append(check,choose,el("small",asset.mime==="application/pdf"?"PDF":"影像"),
            ClinicalUI.assetTools({url:window.LocalSession.resourceURL(scopedPath("/api/analysis/asset?id="+encodeURIComponent(asset.digest))),asset,name,compact:true,
              image:()=>currentUI?.preview.querySelector(`.cataract-asset[data-digest="${asset.digest}"] img`)}));
        }
        file.querySelector("input").checked=window.FileCompare.has(asset.digest);
        file.querySelector("input").disabled=asset.available===false;
        file.querySelector("button").disabled=asset.available===false;
        file.querySelector("button").setAttribute("aria-pressed",String(selected&&asset.digest===currentState.selectedAssetDigest));
        if(choices.children[index]!==file)choices.insertBefore(file,choices.children[index]||null);
      }
    }
  }

  function setMobileView(view, key) {
    if (!currentState || !currentUI) return;
    if (view === "overview") currentState.overviewMobile = key;
    else currentState.mobile = key;
    currentUI[view].dataset.mobile = key;
    for (const control of currentUI[view].querySelectorAll(".cataract-mobile-switch button"))
      control.setAttribute("aria-pressed", String(control.dataset.mobileView === key));
    refitImagePreviews();
  }

  function mobileSwitch(view, state) {
    const nav = el("nav", undefined, "cataract-mobile-switch");
    const options = view === "overview" ? [["soap", "SOAP"], ["numeric", "數值"]] :
      [["numeric", "數值"], ["orders", "醫囑"], ["files", "檔案"]];
    for (const [key, label] of options) {
      const control = button(label, () => setMobileView(view, key), "quiet-button");
      control.dataset.mobileView = key;
      control.setAttribute("aria-pressed", String((view === "overview" ? state.overviewMobile : state.mobile) === key));
      nav.append(control);
    }
    nav.setAttribute("aria-label", view === "overview" ? "SOAP 與數值" : "數值、醫囑與檔案");
    return nav;
  }

  function showView(view) {
    if (!currentUI || !currentState) return;
    currentState.view = view;
    for (const [name, page] of Object.entries(currentUI.pages)) page.hidden = name !== view;
    for (const [name, control] of Object.entries(currentUI.tabs))
      control.setAttribute("aria-pressed", String(name === view));
    const target = view === "overview" ? currentUI.overviewNumeric : currentUI.workspace;
    if (currentUI.numeric.parentElement !== target) target.prepend(currentUI.numeric);
    currentUI.workspace.classList.toggle("numeric-closed", !currentState.numericOpen);
    currentUI.railToggle.setAttribute("aria-expanded", String(currentState.numericOpen));
    currentUI.railToggle.setAttribute("aria-label", currentState.numericOpen ? "收合歷年眼科數值" : "展開歷年眼科數值");
    currentUI.railToggle.textContent = currentState.numericOpen ? "‹" : "›";
    currentUI.railToggle.hidden = view !== "work";
    if (view === "scans") currentUI.scans?.activate();
    refitImagePreviews();
  }

  function render(data) {
    layout();
    const area = $("#analysisResults"), member = data.member, state = stateFor(member);
    const account = embeddedAccount, cohortId = currentCohort?.id;
    const allowedToReadScans = () => currentCohort?.members.find(row => row.mrn === member.mrn)?.account_id === account;
    const scanAllowed = allowedToReadScans();
    const numericSignature = JSON.stringify(data.numeric || []), soapSignature = JSON.stringify(data.latest_soap || {});
    const orderSignature = JSON.stringify(data.orders || []);
    const identitySignature = JSON.stringify([member.mrn, member.name, member.birthday, ClinicalUI.patientAge(member)]);
    if (currentUI && currentUI.scanAllowed === scanAllowed && area.contains(currentUI.heading)) {
      if (currentUI.identitySignature !== identitySignature) {
        const next = patientIdentity(member);currentUI.identity.replaceWith(next);
        currentUI.identity = next;currentUI.identitySignature = identitySignature;
      }
      if (currentUI.numericSignature !== numericSignature) {
        const scroll = currentUI.numeric.scrollTop, next = numericPanel(data, state);
        currentUI.numeric.replaceWith(next); next.scrollTop = scroll;
        currentUI.numeric = next; currentUI.numericSignature = numericSignature;
      }
      if (currentUI.soapSignature !== soapSignature) {
        const next = soapSection(data); currentUI.soap.replaceWith(next);
        currentUI.soap = next; currentUI.soapSignature = soapSignature;
        currentUI.scans?.updateSOAP(soapSection(data));
      }
      if (currentUI.orderSignature !== orderSignature) {
        const scroll = currentUI.orders.scrollTop;
        orderPanel(data, state, currentUI.orders); currentUI.orders.scrollTop=scroll;
        currentUI.orderSignature = orderSignature;
      }
      const updated = (data.orders || []).find(row => row.id === state.selectedOrderId);
      if (updated) {
        state.orderRow = updated;
        if (updated.report_loaded && updated.saved_at !== state.reportSavedAt)
          void readSavedOrder(updated).catch(error => {
            if (currentState === state && state.selectedOrderId === updated.id)
              currentUI?.preview.append(el("p", error.message, "form-error"));
          });
      }
      syncOrderControls();
      syncAttachments();
      if (state.view === "scans") currentUI.scans?.activate();
      return;
    }
    releasePreviews();
    restoreHeaderTools();
    area.replaceChildren();
    if (state.selectedOrderId) {
      state.orderRow = (data.orders || []).find(row => row.id === state.selectedOrderId) || null;
      if (!state.orderRow) {state.selectedOrderId = ""; state.report = null;}
    }
    const heading = el("div", undefined, "cataract-result-heading");
    const identity = patientIdentity(member);heading.append(identity);
    const tabs = {}, nav = el("nav", undefined, "cataract-view-nav");
    nav.setAttribute("aria-label", "術前分析檢視");
    const views = [["overview", "SOAP+數值"], ["work", "數值+醫囑報告"]];
    if (window.CataractScans) views.push(["scans", "SOAP+掃描病歷"]);
    for (const [key, label] of views) {
      const control = button(label, () => showView(key), "quiet-button");
      control.dataset.view = key; control.setAttribute("aria-pressed", String(state.view === key));
      tabs[key] = control; nav.append(control);
    }
    const compare = button("查看比較（0 / 6）", () => window.FileCompare.open(), "quiet-button");
    const retry = button("續跑未完成", () => action(() => start({resume: true})), "quiet-button");
    const railToggle = button("‹", () => {state.numericOpen = !state.numericOpen; showView("work");}, "cataract-rail-toggle quiet-button");
    nav.append(compare, retry, railToggle); heading.append(nav);
    const tools = el("details", undefined, "cataract-header-tools");
    const toolsPanel = el("div", undefined, "cataract-header-tools-panel");
    tools.append(el("summary", "資料與操作"), toolsPanel);
    tools.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        tools.open = false;
        tools.querySelector("summary")?.focus();
      }
    });
    heading.append(tools);
    const overview = el("div", undefined, "cataract-view cataract-overview");
    overview.dataset.mobile = state.overviewMobile;
    const overviewGrid = el("div", undefined, "cataract-overview-grid");
    const soap = soapSection(data), overviewNumeric = el("div", undefined, "cataract-overview-numeric");
    overviewGrid.append(soap, overviewNumeric);
    overview.append(mobileSwitch("overview", state), overviewGrid);
    const workPage = el("div", undefined, "cataract-view cataract-work-view");
    workPage.dataset.mobile = state.mobile;
    const workspace = el("div", undefined, "cataract-workspace");
    const numeric = numericPanel(data, state), orders = orderPanel(data, state);
    const preview = el("section", undefined, "cataract-section cataract-report-preview");
    workPage.append(mobileSwitch("work", state), workspace);
    workspace.append(numeric, orders, preview);
    const pages = {overview, work:workPage};
    const scans = scanAllowed && window.CataractScans?.create({account, cohortId, mrn:member.mrn, soap:soapSection(data),
      readOnly:!!config?.read_only, canFetch:() => !config?.read_only && sharedStatus?.online !== false,
      isCurrent:() => activeView() && currentUI?.scans === scans && scans.element.isConnected &&
        currentCohort?.id === cohortId && $("#analysisPatient").value === member.mrn && allowedToReadScans(),
      biometryRequest:config?.biometry_variant === "basic" ? undefined : values => ap("cataract/biometry", values),
      mountBiometryWorkspace,
      textOnly:config?.biometry_variant === "lite"});
    if (window.CataractScans) pages.scans = scans?.element ||
      el("p", "請將病人抓取帳號設為目前登入帳號。", "cataract-view small muted");
    currentUI = {heading, identity, identitySignature, tools, pages, tabs, compare, retry, scans, scanAllowed,
      overview, overviewNumeric, work:workPage, soap, numeric, orders, preview, workspace, railToggle,
      numericSignature, soapSignature, orderSignature};
    area.append(heading, ...Object.values(pages));
    // Keep the shared controls connected while constructing the new view.
    // Report rendering reads their values, including a restored selection.
    for (const control of [toolbar, coverage]) {
      if (typeof toolsPanel.moveBefore === "function") toolsPanel.moveBefore(control, null);
      else toolsPanel.append(control);
    }
    renderOrder(state.report, state.orderRow);
    showView(state.view);
    syncAttachments();
    const selectedRow = state.orderRow;
    if (selectedRow?.report_loaded && selectedRow.saved_at !== state.reportSavedAt)
      void readSavedOrder(selectedRow).catch(error => {
        if (currentState === state && state.selectedOrderId === selectedRow.id)
          currentUI?.preview.append(el("p", error.message, "form-error"));
      });
  }

  function syncAttachments() {
    if (!activeView()) return;
    for (const checkbox of $$("#analysisResults input[data-compare-digest]"))
      checkbox.checked = window.FileCompare.has(checkbox.dataset.compareDigest);
    if (currentUI) {
      currentUI.scans?.syncControls();
      currentUI.compare.textContent = `查看比較（${window.FileCompare.count()} / 6）`;
      currentUI.compare.disabled = !window.FileCompare.count();
      const row = status?.members?.find(item => item.mrn === $("#analysisPatient").value);
      currentUI.retry.hidden = !row?.attempted || row.cache_cleared || row.ready || !!row.active_run_id;
    }
  }

  function refitImagePreviews() {
    for (const box of $$(".cataract-image-preview")) box.refit?.();
  }

  function imagePreview(asset, url, title) {
    const views = currentState.imageViews;
    if (!views.has(asset.digest)) views.set(asset.digest, {fit:true, zoom:1, left:0, top:0});
    const view = views.get(asset.digest);
    const box = el("div", undefined, "cataract-asset cataract-image-preview");box.dataset.digest = asset.digest;
    const tools = el("div", undefined, "cataract-image-tools"), controls = el("div", undefined, "cataract-image-zoom");
    controls.setAttribute("role", "group");controls.setAttribute("aria-label", "圖片縮放");
    const viewport = el("div", undefined, "cataract-image-viewport");
    let restored = false;
    viewport.tabIndex = 0;viewport.setAttribute("role", "region");viewport.setAttribute("aria-label", "醫囑圖片預覽，可捲動查看");
    const file = el("img");file.title = file.alt = title;
    const fitScale = () => {
      if (!file.naturalWidth || !file.naturalHeight || !viewport.clientWidth || !viewport.clientHeight) return null;
      if (view.fit === "width") return viewport.clientWidth / file.naturalWidth;
      if (view.fit === "height") return viewport.clientHeight / file.naturalHeight;
      return Math.min(1, viewport.clientWidth / file.naturalWidth, viewport.clientHeight / file.naturalHeight);
    };
    function apply() {
      const fit = fitScale();
      if (!box.isConnected || fit === null) return;
      if (view.fit) view.zoom = fit;
      file.style.width = Math.floor(file.naturalWidth * view.zoom * 100) / 100 + "px";
      const percent = Math.round(view.zoom * 100) || +(view.zoom * 100).toPrecision(2);
      reset.textContent = percent + "%";
      reset.setAttribute("aria-label", `目前縮放 ${percent}%，回到最適比例`);
      for (const control of [less, reset, more, ...fitButtons]) control.disabled = false;
      for (const control of fitButtons) control.setAttribute("aria-pressed", String(view.fit === control.dataset.fit));
      if (!restored) {viewport.scrollLeft = view.left;viewport.scrollTop = view.top;restored = true;}
    }
    function step(direction) {
      const fit = fitScale();
      if (fit === null) return;
      const small = view.zoom < .25 || view.zoom === .25 && direction < 0;
      const next = small ? direction < 0 ? view.zoom * .8 : Math.min(.25, view.zoom * 1.25)
        : (direction > 0 ? Math.floor(view.zoom * 4 + 1e-9) + 1 : Math.ceil(view.zoom * 4 - 1e-9) - 1) / 4;
      view.fit = false;view.zoom = Math.max(Math.min(.01, fit / 2), Math.min(4, next));apply();
    }
    const less = button("−", () => step(-1)), more = button("＋", () => step(1));
    less.setAttribute("aria-label", "縮小圖片");less.title = "縮小圖片";
    more.setAttribute("aria-label", "放大圖片");more.title = "放大圖片";
    const reset = button("…", () => {view.fit = true;view.left = view.top = 0;viewport.scrollLeft = viewport.scrollTop = 0;apply();});
    reset.className = "cataract-image-fit";reset.title = "回到最適比例";reset.setAttribute("aria-label", "回到最適比例");
    const fitButtons = [["width", "最適寬度"], ["height", "最適高度"]].map(([mode, label]) => {
      const control = button(label, () => {view.fit = mode;view.left = view.top = 0;viewport.scrollLeft = viewport.scrollTop = 0;apply();});
      control.dataset.fit = mode;control.setAttribute("aria-pressed", String(view.fit === mode));return control;
    });
    for (const control of [less, reset, more, ...fitButtons]) control.disabled = true;
    controls.append(less, reset, more, ...fitButtons);tools.append(el("span", "圖片縮放", "small muted"), controls);
    viewport.append(file);box.append(tools, viewport);box.refit = apply;
    viewport.addEventListener("scroll", () => {view.left = viewport.scrollLeft;view.top = viewport.scrollTop;}, {passive:true});
    file.addEventListener("load", apply);
    file.addEventListener("error", () => {reset.textContent = "載入失敗";for (const control of [less, reset, more, ...fitButtons]) control.disabled = true;});
    file.src = url;
    if (!imageResizeObserver && typeof ResizeObserver === "function") {
      // Observe the stable layout once; detached images are never retained.
      imageResizeObserver = new ResizeObserver(refitImagePreviews);imageResizeObserver.observe($("#cataractLayout"));
    }
    return box;
  }

  function createPdfExtraction(biometry, doc, viewer) {
    const element = el("div", undefined, "cataract-pdf-extraction");
    const extract = button("擷取PDF", run, "quiet attachment-control");
    const category = el("select");
    for (const [value, label] of window.CataractBiometry.categories) {
      const option = el("option", label);
      option.value = value;
      category.append(option);
    }
    category.value = "all";
    const categoryLabel = el("label", "擷取內容", "cataract-pdf-category");
    categoryLabel.append(category);
    const group = el("fieldset", undefined, "cataract-pdf-pages");
    const numbers = el("div", undefined, "cataract-pdf-page-choices");
    const status = el("p", undefined, "caption cataract-pdf-status");
    status.setAttribute("role", "status");
    const retry = button("重讀核對結果", () => biometry.reloadDocument(doc), "quiet-button");
    retry.hidden = true;
    const panel = el("aside", undefined, "cataract-inline-review");
    const review = button("檢核結果", () => setReviewOpen(true), "quiet-button");
    const close = button("收合", () => {setReviewOpen(false); review.focus();}, "quiet-button");
    close.setAttribute("aria-label", "收合擷取結果");
    panel.hidden = review.hidden = true;
    const selected = new Set();
    let file = null, job = {}, loading = false, pageError = "", startError = "", disposed = false;
    let unsubscribe, editor = null, reviewedJob = "";

    function checkbox(text, ariaLabel, update) {
      const label = el("label", undefined, "check-label");
      const input = el("input");
      input.type = "checkbox";
      input.setAttribute("aria-label", ariaLabel);
      input.addEventListener("change", () => {update(input.checked); render();});
      label.append(input, el("span", text));
      return [label, input];
    }

    const [wholeLabel, whole] = checkbox("整份", "整份 PDF", checked => {
      if (checked) {
        selected.clear();
        for (const input of numbers.querySelectorAll("input")) input.checked = false;
      }
    });
    whole.checked = true;
    group.append(el("legend", "擷取頁碼", "sr-only"), wholeLabel, numbers);
    element.append(extract, categoryLabel, group, review, retry, status);

    function setReviewOpen(open) {
      panel.hidden = !open;
      review.hidden = !reviewedJob || open;
    }

    function renderReview() {
      const documents = job.result?.documents;
      const index = documents?.findIndex(row => row.id === doc.id && row.digest === doc.digest) ?? -1;
      if (index < 0 || !job.jobId) {
        if (reviewedJob) panel.replaceChildren();
        reviewedJob = "";
        editor = null;
        setReviewOpen(false);
        return;
      }
      if (reviewedJob === job.jobId || !window.CataractBiometryReview) return;
      const jobId = reviewedJob = job.jobId;
      const navigate = page => {
        if (disposed || reviewedJob !== jobId || !viewer?.isConnected || !Number.isSafeInteger(page) || page < 1) return;
        const url = ClinicalUI.pdfURL(viewer.src, page);
        if (viewer.src !== url) viewer.src = url;
      };
      editor = window.CataractBiometryReview.create({result:job.result, ...biometry.reviewOptions(job.result),
        documentIndex:index, current:() => !disposed && element.isConnected && reviewedJob === jobId,
        onPageChange:navigate});
      editor.header.append(close);
      panel.replaceChildren(editor.element);
      setReviewOpen(true);
      if (documents[index].pages?.length === 1) navigate(documents[index].pages[0]);
    }

    function render() {
      group.hidden = !file;
      extract.textContent = pageError ? "重讀頁數" : "擷取PDF";
      extract.disabled = loading || !pageError && (!file || job.busy || !whole.checked && !selected.size);
      category.disabled = loading || job.busy;
      renderReview();
      retry.hidden = !job.restoreError || job.restoring || !biometry.reloadDocument;
      const savedMessage = job.changed ? "原始 PDF 已變更，請重新擷取。" : job.restoreError ||
        (job.restoring ? "正在讀取本機核對結果…" : job.saved ? "已載入本機擷取與核對結果。" : "");
      const message = [pageError || startError || (loading ? "正在讀取 PDF 頁數…" : job.message), savedMessage]
        .filter(Boolean).join(" ");
      if (status.textContent !== message) status.textContent = message;
      status.hidden = !message;
      status.classList.toggle("form-error", !!(pageError || startError || job.restoreError || job.changed || job.error || job.state === "failed"));
    }

    async function loadPages() {
      loading = true;
      pageError = "";
      render();
      try {
        const pages = await biometry.readPages(doc, () => !disposed && element.isConnected);
        if (disposed) return;
        if (!pages) throw new Error("無法讀取此 PDF 的頁數。");
        file = {id:doc.id, digest:pages.digest || doc.digest};
        for (let page = 1; page <= pages.count; page++) {
          const [label] = checkbox(String(page), `第 ${page} 頁`, checked => {
            if (checked) {selected.add(page); whole.checked = false;}
            else selected.delete(page);
          });
          numbers.append(label);
        }
      } catch (error) {
        if (!disposed) pageError = error.message || "無法讀取此 PDF 的頁數。";
      } finally {
        loading = false;
        if (!disposed) render();
      }
    }

    async function run() {
      if (disposed || extract.disabled) return;
      if (pageError) return loadPages();
      startError = "";
      try {
        await biometry.startDocument(file, whole.checked ? undefined : [...selected], category.value);
      } catch (error) {
        if (!disposed) startError = error.message || "無法安排背景擷取。";
      } finally {
        if (!disposed) render();
      }
    }

    return {element, panel,
      activate() {
        unsubscribe = biometry.subscribeDocument(doc, next => {job = next; render();});
        void loadPages();
      },
      refreshReview() {editor?.refresh();},
      dispose() {disposed = true; unsubscribe?.(); element.remove(); panel.remove();}};
  }

  function clearOrderExtraction() {
    currentUI.orderExtraction?.dispose();
    currentUI.orderExtraction = null;
  }

  function renderOrder(value, fallback) {
    const area = currentUI?.preview, row = value?.order || fallback;
    if (!area) return;
    const assets = value?.assets || [];
    const selected = assets.find(asset => asset.digest === currentState.selectedAssetDigest) || assets[0];
    const previous=area.querySelector(".cataract-asset");
    const retained = previous?.dataset.digest === selected?.digest ? previous : null;
    const biometry = selected?.mime === "application/pdf" && !config?.read_only ? currentState.orderBiometry : null;
    if (currentUI.orderExtraction &&
        (!retained || currentUI.orderExtraction.id !== row?.id || currentUI.orderExtraction.biometry !== biometry))
      clearOrderExtraction();
    for(const child of [...area.children])if(child!==retained)child.remove();
    area.append(el("h3", "醫囑報告", "sr-only"));
    if (!row) {
      area.removeAttribute("aria-label");
      syncOrderControls();
      area.append(el("p", "從醫囑清單選取一筆，這裡會顯示附件與報告。", "empty-result"));
      return;
    }
    area.setAttribute("aria-label", [row.name, row.date, row.case_no].filter(Boolean).join(" · "));
    let pacs=null;
    if(!config?.read_only&&embeddedAccount){
      const state=currentState,sourceAccount=embeddedAccount,cohort=currentCohort.id;
      const scope={account:sourceAccount,cohort_id:cohort,mrn:$("#analysisPatient").value,resource:"order_report",reference:row.id};
      pacs=ClinicalUI.pacsControl({scope,enabled:sharedStatus?.online!==false,
        current:()=>currentState===state&&embeddedAccount===sourceAccount&&currentCohort?.id===cohort&&state.selectedOrderId===row.id&&!config?.read_only&&sharedStatus?.online!==false,
        request:values=>api("/api/reviews/pacs/open",values)});
      area.prepend(pacs);
    }
    if (!value) {
      syncOrderControls();
      area.append(el("p", "此報告仍在抓取或尚未保存。", "small muted"));
      syncAttachments();
      return;
    }
    if (selected) {
      currentState.selectedAssetDigest = selected.digest;
      syncOrderControls();
      let box = retained;
      if (!box) {
        const url = window.LocalSession.resourceURL(scopedPath("/api/analysis/asset?id=" + encodeURIComponent(selected.digest)));
        if (selected.mime === "application/pdf") {
          box = el("div", undefined, "cataract-asset");box.dataset.digest = selected.digest;
          const file = el("iframe");file.src = ClinicalUI.pdfURL(url);file.title = `${row.name} · 所選檔案`;box.append(file);
        } else box = imagePreview(selected, url, `${row.name} · 所選檔案`);
        if (pacs) pacs.after(box); else area.prepend(box);
      }
      if (biometry && !currentUI.orderExtraction) {
        const extraction = createPdfExtraction(biometry, {id:row.id, digest:selected.digest}, box.querySelector("iframe"));
        currentUI.orderExtraction = {...extraction, id:row.id, biometry};
        box.prepend(extraction.element);
        currentUI.workspace.append(extraction.panel);
        extraction.activate();
      }
      refitImagePreviews();
    } else syncOrderControls();
    if(value.texts?.length||value.details?.length)area.append(ClinicalUI.textDownload(value,
      [row.date,row.name||"醫囑報告"].filter(Boolean).join(" ")));
    for (const report of value.texts || []) {
      const detail = el("details"), summary = el("summary", "文字報告");
      detail.append(summary, el("pre", report.text || Object.entries(report.fields || {})
        .map(([key, item]) => `${key}：${item}`).join("\n"))); area.append(detail);
    }
    for (const detail of value.details || []) if (Object.keys(detail.fields || {}).length) {
      const block = el("details"); block.append(el("summary", "醫囑明細"),
        el("pre", Object.entries(detail.fields).map(([key, item]) => `${key}：${item}`).join("\n")));
      area.append(block);
    }
    if (value.issues?.length) area.append(el("p", value.issues.map(item => item.message).join("；"), "form-error"));
    if (!value.assets?.length && !value.texts?.length && !value.details?.length)
      area.append(el("p", "此醫囑沒有可顯示的報告內容。", "small muted"));
    syncAttachments();
  }

  async function readSavedOrder(row) {
    const sequence = ++orderSequence, key = currentKey, state = currentState;
    const request = {cohort_id: currentCohort.id, mrn: $("#analysisPatient").value,
      resource: "order_report", reference: row.id};
    const stored = await api("/api/reviews/history/read", request);
    if (sequence !== orderSequence || key !== currentKey || state.selectedOrderId !== row.id) return;
    state.report = stored.data;
    state.reportSavedAt = stored.updated_at || "";
    renderOrder(stored.data, row);
  }

  async function openOrder(row, assetDigest="") {
    const member = currentCohort?.members.find(item => item.mrn === $("#analysisPatient").value);
    if (!member || member.account_id !== embeddedAccount) throw new Error("此病人未指定給目前登入帳號。");
    const state = currentState;
    if(state.selectedOrderId===row.id&&state.report){state.selectedAssetDigest=assetDigest||state.selectedAssetDigest;setMobileView("work","files");showView("work");renderOrder(state.report,row);return;}
    state.selectedOrderId = row.id; state.orderRow = row; state.report = null; state.reportSavedAt = "";
    state.selectedAssetDigest = assetDigest;
    setMobileView("work", "files");
    showView("work");
    for (const control of $$(".cataract-order-row"))
      control.setAttribute("aria-current", String(control.dataset.orderId === row.id));
    renderOrder(null, row);
    await readSavedOrder(row);
  }

  async function refreshOrder(row) {
    const member = currentCohort?.members.find(item => item.mrn === $("#analysisPatient").value);
    if (!member || member.account_id !== embeddedAccount) throw new Error("病人或帳號已切換。");
    const request = {cohort_id: currentCohort.id, mrn: member.mrn,
      resource: "order_report", reference: row.id};
    const progress = el("div", undefined, "cataract-order-progress");
    const item = [...currentUI.orders.querySelectorAll(".cataract-order-item")]
      .find(candidate => candidate.dataset.orderId === row.id);
    (item || currentUI.orders).append(progress);
    JobProgress.pending(progress, "醫囑報告", "正在更新此報告與附件…");
    try {
      const started = await api("/api/tasks/start", {kind: "history", ...request, force: true});
      parent.postMessage({type: "bot:task-started"}, location.origin);
      for (;;) {
        await JobProgress.ready();
        const task = await api("/api/tasks/detail?summary=1&id=" + encodeURIComponent(started.task_id));
        JobProgress.show(progress, task);
        if (!active.has(task.status)) {
          if (currentState.selectedOrderId === row.id) await readSavedOrder(row);
          await loadAnalysisResult();
          return;
        }
        await JobProgress.wait();
      }
    } finally {progress.remove();}
  }

  window.addEventListener("message",event=>{
    if(event.origin!==location.origin||event.source!==parent||event.data?.type!=="bot:data-cleared"||event.data.account!==embeddedAccount)return;
    const cleared=event.data.mrns||[];
    for (const [key, cached] of resultCache) if (cleared.includes(cached.data.member.mrn)) resultCache.delete(key);
    for (const key of resultReads.keys()) if (cleared.includes(key.split(":").at(-1))) resultReads.delete(key);
    statusAt = 0;
    for(const [key,state] of patientStates)if(cleared.includes(key.split(":").at(-1))){state.report=null;state.reportSavedAt="";state.orderRow=null;state.selectedOrderId="";state.selectedAssetDigest="";state.orderBiometry?.clear();}
    if(cleared.includes($("#analysisPatient").value)){orderSequence++;resultRequest++;releasePreviews();currentUI=null;restoreHeaderTools();$("#analysisResults").replaceChildren();window.ScanBrowser?.reset();action(()=>loadAnalysisResult());}
  });
  $("#cataractUpdate").addEventListener("click", () => action(() => start({refresh: true})));
  document.addEventListener("pointerdown", event => {
    const tools = currentUI?.tools;
    if (tools?.open && !tools.contains(event.target)) tools.open = false;
  });
  window.addEventListener("filecomparechange", syncAttachments);
  window.addEventListener("filecomparemessage", event => {
    if (!activeView()) return;
    currentUI?.scans?.comparisonFailed();
    notify(event.detail, true);
  });
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== parent ||
        event.data?.type !== "bot:workspace-resumed" || !activeView()) return;
    const member = currentCohort?.members.find(item => item.mrn === $("#analysisPatient").value);
    if (member) window.FileCompare.context(embeddedAccount, member.mrn, (window.ClinicalUI?.patientName?.(member.name) ?? member.name) || "");
    const scroll = event.data.scroll;
    action(async () => {
      await loadAnalysisResult();
      if (!scroll || scroll.account !== embeddedAccount || scroll.cohort !== currentCohort?.id) return;
      requestAnimationFrame(() => {
        for (const selector of ["#analysisResults", ".cataract-numeric", ".cataract-orders", ".cataract-report-preview"])
          if (Number.isFinite(scroll.regions?.[selector])) {
            const area = document.querySelector(selector);
            if (area) area.scrollTop = scroll.regions[selector];
          }
        for (const [exam, top] of scroll.exams || []) {
          if (!Number.isFinite(top)) continue;
          const group = [...document.querySelectorAll(".cataract-numeric-group")]
            .find(item => item.dataset.exam === exam);
          const table = group?.querySelector(".cataract-numeric-table-wrap");
          if (table) table.scrollTop = top;
        }
        window.scrollTo(scroll.x || 0, scroll.y || 0);
      });
    });
  });
  return {layout, refreshStatus, renderPatients, render, cachedResult, readResult, preparePatient, readFailure, createPdfExtraction,
    autoStart: ({selectedOnly=false}={}) => enqueue(selectedOnly?"selected":"start")};
})();
