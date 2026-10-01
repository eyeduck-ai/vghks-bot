"use strict";
window.CataractExport = (() => {
  let modal = null, serial = 0, sending = false;
  function mount() {
    if (modal) return;
    modal = el("dialog");modal.id = "cataractExportDialog";modal.setAttribute("aria-labelledby", "cataractExportTitle");
    const heading = el("div", undefined, "dialog-heading"), title = el("h2", "匯出白內障術前報告");
    title.id = "cataractExportTitle";
    heading.append(title, button("×", () => modal.close()));heading.lastChild.setAttribute("aria-label", "關閉匯出視窗");
    const body = el("div", undefined, "record-body");body.id = "cataractExportContent";
    modal.append(heading, body);document.body.append(modal);
    modal.addEventListener("close", () => {serial++;});
  }
  async function open() {
    if (!currentCohort || sending) return;
    mount();
    const cohort = currentCohort.id, request = ++serial, selectedMrn = $("#analysisPatient").value;
    const content = $("#cataractExportContent");content.replaceChildren(el("p", "正在核對已存資料…", "small muted"));
    modal.showModal();
    try {
      const result = await api("/api/analysis/export/read", {cohort_id:cohort});
      if (request !== serial || !modal.open || currentCohort?.id !== cohort) return;
      content.replaceChildren(el("p", "包含已存 SOAP、歷年數值、指定醫囑文字與原始 PDF／圖片。解壓縮後開啟 index.html 可離線閱讀。", "small muted"));
      content.append(el("p", "只匯出本機已存資料；尚未取得的內容會標示於報告中。", "small muted"));
      const controls = el("div", undefined, "actions"), list = el("div", undefined, "export-patients");
      const selected = new Set(), count = el("span", "", "small muted"), status = el("p", "", "small");
      status.setAttribute("role", "status");
      const save = button("下載報告 ZIP", async () => {
        if (!selected.size || sending) return;
        if (currentCohort?.id !== cohort) {status.textContent = "病人清單已切換，請重新開啟匯出。";return;}
        sending = true;save.disabled = true;save.setAttribute("aria-busy", "true");
        for (const control of content.querySelectorAll("input,button")) control.disabled = true;
        status.textContent = "正在整理已存資料與附件…";
        try {
          const response = await fetch(scopedPath("/api/analysis/export"), {method:"POST", credentials:"same-origin", cache:"no-store",
            headers:{"Content-Type":"application/json", "X-CSRF-Token":config?.csrf || "", "X-Database-Context":config?.context || ""},
            body:JSON.stringify({cohort_id:cohort, mrns:[...selected]})});
          if (!response.ok) {const error = await response.json();throw new Error(error.error || "報告匯出未完成。");}
          const encodedName = response.headers.get("Content-Disposition")?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
          const name = encodedName ? decodeURIComponent(encodedName) : "白內障術前報告.zip";
          ClinicalUI.saveBlob(await response.blob(), name);
          status.textContent = "已送出下載，請將 ZIP 完整解壓縮後開啟 index.html。";
        } catch (error) {status.textContent = error.message;}
        finally {
          sending = false;save.removeAttribute("aria-busy");
          for (const control of content.querySelectorAll("input,button")) control.disabled = false;
          update();
        }
      }, "primary");
      function update() {count.textContent = `已選 ${selected.size} 位`;save.disabled = !selected.size || sending;}
      const rows = result.members.filter(member => member.available);
      for (const member of rows) {
        const row = el("label", undefined, "export-patient-row"), check = el("input");check.type = "checkbox";
        check.value = member.mrn;check.checked = member.mrn === selectedMrn;
        if (check.checked) selected.add(member.mrn);
        check.addEventListener("change", () => {if (check.checked) selected.add(member.mrn);else selected.delete(member.mrn);update();});
        const info = el("span");info.append(el("strong", [member.name, member.mrn].filter(Boolean).join(" · ")),
          el("small", `${member.soap ? "有 SOAP" : "SOAP 未存"} · ${member.numeric_rows} 筆數值 · ${member.orders} 筆醫囑 · ${member.attachments} 份附件`));
        row.append(check, info);list.append(row);
      }
      const chooseAll = button("全選", () => {for (const check of list.querySelectorAll("input")) {check.checked = true;selected.add(check.value);}update();});
      const clear = button("清除選取", () => {selected.clear();for (const check of list.querySelectorAll("input")) check.checked = false;update();});
      controls.append(chooseAll, clear, count);content.append(controls, list);
      if (!rows.length) content.append(el("p", "此清單尚無已保存的 SOAP、數值或醫囑報告。", "empty-result"));
      const actions = el("div", undefined, "dialog-actions");actions.append(save);content.append(status, actions);update();
    } catch (error) {if (request === serial) content.replaceChildren(el("p", error.message, "form-error"));}
  }
  return {open};
})();
