"use strict";
window.ClinicalUI = (() => {
  function pdfURL(path) {
    const url = new URL(path, location.href);
    const options = new URLSearchParams(url.hash.slice(1));
    options.set("navpanes", "0"); options.set("toolbar", "1");
    url.hash = options.toString();
    return url.href;
  }
  function mrnBadge(mrn, announce = () => {}) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "patient-mrn-copy review-mrn-copy";
    button.textContent = mrn; button.title = "點擊複製病歷號";
    button.setAttribute("aria-label", "複製病歷號 " + mrn);
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(mrn);
        announce("已複製病歷號");
      } catch (_) {
        const input = document.createElement("input");
        input.className = "patient-mrn-fallback"; input.readOnly = true; input.value = mrn;
        input.setAttribute("aria-label", "病歷號，請手動複製");
        button.replaceWith(input); input.focus(); input.select();
        announce("請複製已選取的病歷號（Ctrl+C）");
      }
    });
    return button;
  }
  function fileName(name, mime) {
    const ext = {"application/pdf":"pdf", "image/jpeg":"jpg", "image/png":"png", "image/gif":"gif"}[mime] || "txt";
    let safe = String(name || "報告").replace(/[\x00-\x1f\x7f<>:"/\\|?*]/g, "_").trim().replace(/[. ]+$/, "");
    safe = safe.replace(new RegExp("\\." + ext + "$", "i"), "").slice(0, 120) || "報告";
    return safe + "." + ext;
  }
  function downloadLink(url, name, mime, compact = false) {
    const link = document.createElement("a"), path = new URL(url, location.href);
    const label = fileName(name, mime);
    path.searchParams.set("download", "1");path.searchParams.set("name", label);path.hash = "";
    link.href = path.href;link.download = label;
    link.textContent = compact ? "↓" : "下載原檔";
    link.title = "下載 " + label;link.setAttribute("aria-label", link.title);
    link.className = "attachment-control";
    link.addEventListener("click", async event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (link.dataset.busy) return;
      link.dataset.busy = "true";link.setAttribute("aria-busy", "true");
      try {
        // The same download path handles PDF and images, including embedded browsers.
        const response = await fetch(path.href, {credentials:"same-origin",cache:"no-store"});
        if (!response.ok) {const error = await response.json();throw new Error(error.error || "附件下載未完成。");}
        saveBlob(await response.blob(), label);
      } catch (error) {
        const message = document.createElement("span");message.className = "attachment-copy-status";
        message.setAttribute("role", "status");message.textContent = error.message;
        link.after(message);setTimeout(() => message.remove(), 8000);
      } finally {delete link.dataset.busy;link.removeAttribute("aria-busy");}
    });
    return link;
  }
  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url;link.download = name;document.body.append(link);link.click();link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  function reportText(value) {
    const order = value.order || value;
    const parts = [[order.date, order.name, order.case_no].filter(Boolean).join(" · ")];
    for (const [label, rows] of [["文字報告", value.texts || []], ["醫囑明細", value.details || []]])
      for (const row of rows) {
        parts.push("\n" + label);
        if (row.text) parts.push(row.text);
        for (const [key, content] of Object.entries(row.fields || {})) parts.push(`${key}：${content ?? ""}`);
      }
    return parts.join("\n") + "\n";
  }
  function textDownload(value, name) {
    const button = document.createElement("button");button.type = "button";
    button.className = "attachment-control";button.textContent = "下載文字報告";
    button.addEventListener("click", () => saveBlob(new Blob(["\ufeff", reportText(value)],
      {type:"text/plain;charset=utf-8"}), fileName(name, "text/plain")));
    return button;
  }
  async function imagePNG(image, url) {
    const source = typeof image === "function" ? image() : image;
    const img = source?.tagName === "IMG" ? source : new Image();
    if (img !== source) img.src = url;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;canvas.height = img.naturalHeight;
    canvas.getContext("2d").drawImage(img, 0, 0);
    return await new Promise((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error("圖片無法複製。")), "image/png"));
  }
  function assetTools({url, asset, name, image, compact = false}) {
    const area = document.createElement("span");area.className = "attachment-actions";
    area.append(downloadLink(url, name, asset.mime, compact));
    if (asset.mime.startsWith("image/")) {
      const copy = document.createElement("button");copy.type = "button";
      copy.className = "attachment-control";copy.textContent = compact ? "⧉" : "複製圖片";
      copy.title = "複製圖片";copy.setAttribute("aria-label", "複製圖片 " + name);
      const status = document.createElement("span");status.className = "attachment-copy-status";
      status.setAttribute("role", "status");status.hidden = true;
      let timer;
      copy.addEventListener("click", async () => {
        copy.disabled = true;clearTimeout(timer);
        try {
          if (!navigator.clipboard?.write || !window.ClipboardItem ||
              ClipboardItem.supports && !ClipboardItem.supports("image/png")) throw new Error("unsupported");
          // Invoke write during the user's click; conversion finishes in the item's Promise.
          await navigator.clipboard.write([new ClipboardItem({"image/png": imagePNG(image, url)})]);
          status.textContent = "已複製圖片";
        } catch (_) {
          status.textContent = "請在圖片上按右鍵選「複製圖片」，或下載原檔。";
        } finally {
          copy.disabled = false;status.hidden = false;
          timer = setTimeout(() => {status.hidden = true;}, 8000);
        }
      });
      area.append(copy, status);
    }
    return area;
  }
  function syncComparison() {
    for (const checkbox of document.querySelectorAll("input[data-compare-digest]"))
      checkbox.checked = !!window.FileCompare?.has(checkbox.dataset.compareDigest);
  }
  function comparisonChoice(item) {
    const label = document.createElement("label");label.className = "attachment-compare";
    const input = document.createElement("input");input.type = "checkbox";input.dataset.compareDigest = item.digest;
    input.setAttribute("aria-label", "將 " + item.name + " 加入比較");
    input.checked = !!window.FileCompare?.has(item.digest);
    const message = document.createElement("span");message.className = "attachment-copy-status";
    message.setAttribute("role", "status");message.hidden = true;
    input.addEventListener("change", () => {
      try {
        if (input.checked) window.FileCompare.add(item);else window.FileCompare.remove(item.digest);
        message.hidden = true;
      } catch (error) {message.textContent = error.message;message.hidden = false;}
      syncComparison();
    });
    label.append(input, document.createTextNode("加入比較"), message);
    return label;
  }
  window.addEventListener("filecomparechange", syncComparison);
  return {pdfURL, mrnBadge, fileName, downloadLink, saveBlob, reportText, textDownload, assetTools, comparisonChoice};
})();
