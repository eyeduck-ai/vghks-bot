"use strict";

// Completed tokens are looked up immediately. A pasted list shares one SDK task.
window.PatientTokens = (() => {
  const separator = /[\s,;，；、]/u;
  const split = /[\s,;，；、]+/u;

  function createErrors({container, onOpen, onRetry}) {
    const host = document.getElementById(container), errors = new Map();
    function render() {
      host.replaceChildren(...[...errors].map(([key, error]) => {
        const row = document.createElement("div"), message = document.createElement("span");
        row.className = "patient-lookup-error";
        message.textContent = error.token + "：" + error.message;
        const action = document.createElement("button");
        action.type = "button";
        action.className = "quiet";
        action.textContent = error.taskId ? "查看 DEBUG" : "爬蟲紀錄";
        action.addEventListener("click", () => onOpen(error.taskId));
        row.append(message, action);
        if (onRetry) {
          const retry = document.createElement("button");
          retry.type = "button"; retry.className = "quiet"; retry.textContent = "重試此筆";
          retry.addEventListener("click", () => onRetry(error.token, error.mode));
          row.append(retry);
        }
        const dismiss = document.createElement("button");
        dismiss.type = "button"; dismiss.className = "quiet"; dismiss.textContent = "移除";
        dismiss.setAttribute("aria-label", "移除失敗紀錄 " + error.token);
        dismiss.addEventListener("click", () => remove(key)); row.append(dismiss);
        return row;
      }));
    }
    function remove(key) {
      errors.delete(key);
      render();
    }
    return {
      add(token, message, mode, taskId = "") {
        const key = mode + ":" + token;
        errors.set(key, {token, message, taskId, mode});
        render();
      },
      remove,
      clear() {
        errors.clear(); render();
      },
    };
  }

  function create({input, kind, request, canLookup, status, onResolved, onFailed, idleLookupMs = 0}) {
    const field = document.getElementById(input);
    const progress = document.getElementById(status);
    let queue = [], busy = false, generation = 0, idleTimer = 0, composing = false;
    const pending = new Set();

    function enqueue(values, mode = kind()) {
      for (const raw of values) {
        const token = raw.normalize("NFKC").trim();
        if (!token) continue;
        if (token.length > 32) {onFailed(token, "識別號碼最多 32 字。", mode); continue;}
        const key = mode + ":" + token.toLocaleUpperCase();
        if (pending.has(key)) continue;
        pending.add(key);
        queue.push({token, mode, key});
      }
      if (queue.length && !busy) void pump();
    }

    function consume(force = false, value = field.value) {
      if (!value || !force && !separator.test(value)) return;
      const parts = value.split(split);
      const complete = force || separator.test(value.at(-1));
      const remainder = complete ? "" : parts.pop() || "";
      field.value = remainder;
      enqueue(parts.filter(Boolean));
    }

    function scheduleIdle() {
      clearTimeout(idleTimer);
      if (!idleLookupMs || !field.value.trim() || composing) return;
      idleTimer = setTimeout(() => {idleTimer = 0; if (!composing) consume(true);}, idleLookupMs);
    }

    async function pump() {
      if (busy || !queue.length) return;
      busy = true;
      const revision = generation, mode = queue[0].mode, batch = [];
      while (queue.length && queue[0].mode === mode && batch.length < 100) batch.push(queue.shift());
      const received = new Set();
      let taskId = "", delivered = 0;
      JobProgress.pending(progress, "病人基本資料", "正在核對 " + batch.length + " 位病人…");
      try {
        if (!canLookup()) throw new Error("此帳號目前無法查詢，請連線後重試。");
        const started = await request("/tasks/start", {
          kind:"resolve", identifier_kind:mode, identifiers:batch.map(row => row.token).join("\n"),
        });
        taskId = started.task_id;
        if (revision !== generation) return;
        for (;;) {
          await JobProgress.ready();
          if (revision !== generation) return;
          let task = await request("/tasks/detail?summary=1&id=" + encodeURIComponent(started.task_id));
          if (revision !== generation) return;
          if (!["queued","running","cancelling"].includes(task.status) || task.done > delivered) {
            await JobProgress.ready();
            if (revision !== generation) return;
            task = await request("/tasks/detail?id=" + encodeURIComponent(started.task_id));
            delivered = task.done || 0;
          }
          if (revision !== generation) return;
          JobProgress.show(progress, task);
          for (const item of task.items || []) {
            const key = (item.input || item.mrn || "").normalize("NFKC").toLocaleUpperCase();
            if (item.status === "resolved" && !received.has(key)) {
              received.add(key);
              onResolved(item, mode);
            }
          }
          if (!["queued","running","cancelling"].includes(task.status)) {
            for (const item of task.items || []) {
              const key = (item.input || item.mrn || "").normalize("NFKC").toLocaleUpperCase();
              if (item.status !== "resolved") {
                received.add(key);
                onFailed(item.input || item.mrn, item.message || task.message || "病人資料未取得。", mode, taskId);
              }
            }
            for (const row of batch) if (!received.has(row.token.toLocaleUpperCase())) onFailed(row.token, task.message || "病人資料未取得。", mode, taskId);
            if (task.status !== "completed") {
              const terminal = progress.firstElementChild;
              setTimeout(() => {if (progress.firstElementChild === terminal) progress.replaceChildren();}, 15000);
            }
            break;
          }
          await JobProgress.wait();
        }
      } catch (error) {
        if (revision === generation) {
          progress.replaceChildren();
          for (const row of batch) if (!received.has(row.token.toLocaleUpperCase())) onFailed(row.token, error.message || "查詢失敗。", mode, taskId);
        }
      } finally {
        if (revision === generation) {
          for (const row of batch) pending.delete(row.key);
          busy = false;
          if (queue.length) void pump();
        }
      }
    }

    field.addEventListener("compositionstart", () => {composing = true; clearTimeout(idleTimer);});
    field.addEventListener("input", event => {if (!event.isComposing && !composing) {consume(); scheduleIdle();}});
    field.addEventListener("compositionend", () => {composing = false; consume(); scheduleIdle();});
    field.addEventListener("keydown", event => {
      if (event.key !== "Enter" || event.isComposing || composing) return;
      event.preventDefault();
      clearTimeout(idleTimer);
      consume(true);
    });
    field.addEventListener("paste", event => {
      const pasted = event.clipboardData?.getData("text") || "";
      if (!separator.test(pasted)) return;
      event.preventDefault();
      clearTimeout(idleTimer);
      const start = field.selectionStart ?? field.value.length, end = field.selectionEnd ?? start;
      consume(true, field.value.slice(0,start) + pasted + field.value.slice(end));
    });

    return {
      add: (values, mode) => enqueue(values, mode),
      reset() {
        generation++;
        clearTimeout(idleTimer);
        composing = false;
        queue = [];
        pending.clear();
        busy = false;
        field.value = "";
        progress.replaceChildren();
      },
      focus: () => field.focus(),
    };
  }

  return {create, createErrors};
})();
