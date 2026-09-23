"use strict";
// Share the proven comparison and sheet editors inside the account workbench.
if (/^[a-f0-9]{32}$/.test(embeddedAccount || "")) {
  document.body.classList.add("tool-embedded");
  document.documentElement.dataset.density = "compact";
  window.addEventListener("message", event => {
    if(event.origin===location.origin&&event.source===parent&&event.data?.type==="bot:soap-updated")action(()=>loadSurgery());
  });
  window.addEventListener("workspace-ready", () => action(async () => {
    const query = new URLSearchParams(location.search);
    const module = ["retina", "cataract", "surgery"].includes(query.get("module")) ? query.get("module") : "retina";
    const names = {retina:"視網膜比較", cataract:"白內障術前比較", surgery:"刀表更新"};
    document.title = names[module] + " · VGHKS-bot";
    $("#analysisTitle").textContent = names[module];
    $("#googleSettingsButton").hidden = $("#sheetHistoryButton").hidden = module !== "surgery";
    $(".analysis-run-controls").previousElementSibling.textContent = "檢查期間與資料更新";
    for (const input of $$("input[name=analysisModule]")) {
      input.checked = input.value === module;
      input.parentElement.lastChild.textContent = names[input.value];
    }
    for (const option of $("#analysisView").options) option.textContent = names[option.value];
    $("#analysisView").value = module;
    $("#analysisView").disabled = true;
    $("#analysisView").closest("label").hidden = true;
    $("#startAnalysis").textContent = "抓取並比較";
    if (module === "surgery") $("#startAnalysis").textContent = "補充院內手術資料";
    $("#saveCohort").textContent = "儲存清單";
    $(".analysis-step-heading").hidden = true;
    showPane("analysis");
    await loadAnalysis(query.get("cohort"));
  }));
}
