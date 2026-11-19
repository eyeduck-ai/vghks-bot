"use strict";

window.ReviewArchive = (() => {
  const limit = 100;

  function fileName(response) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const encoded = /(?:^|;)\s*filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
    let name = /(?:^|;)\s*filename="([^"\r\n]+)"/i.exec(disposition)?.[1] || "";
    if (encoded) {
      try {name = decodeURIComponent(encoded.trim());} catch (_) { /* Use the ASCII filename. */ }
    }
    return /[\\/\x00-\x1f\x7f]/.test(name) || !/\.zip$/i.test(name) ? "VGHKS-原檔.zip" : name;
  }

  async function download({request, scope, ids, current, signal}) {
    if (!current()) return null;
    const response = await request("/reviews/history/export", {...scope, ids}, {raw: true, signal});
    if (!current()) {
      await response.body?.cancel().catch(() => {});
      return null;
    }
    if (response.headers.get("Content-Type")?.split(";", 1)[0].trim() !== "application/zip")
      throw new Error("下載回應不是 ZIP 檔案，請重新打包。");
    const blob = await response.blob();
    if (!current()) return null;
    window.ClinicalUI.saveBlob(blob, fileName(response));
    return {files: Number(response.headers.get("X-VGHKS-Archive-Files")) || 0,
      missing: Number(response.headers.get("X-VGHKS-Archive-Missing")) || 0};
  }

  return {limit, download};
})();
