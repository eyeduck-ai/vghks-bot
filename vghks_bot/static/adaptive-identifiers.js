"use strict";

// Completed tokens are looked up immediately. A pasted list shares one SDK task.
window.PatientTokens = (() => {
  const separator = /[\s,;，；、]/u;
  const split = /[\s,;，；、]+/u;
  const normalize = value => String(value || "").normalize("NFKC").trim();
  const tokenKey = (token, mode) => mode + ":" + normalize(token).toUpperCase();

  function createErrors({container, onOpen, onRetry, onChange}) {
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
      onChange?.({count: errors.size});
    }
    function remove(key) {
      const colon = String(key).indexOf(":");
      errors.delete(colon < 0 ? key : tokenKey(String(key).slice(colon + 1), String(key).slice(0, colon)));
      render();
    }
    return {
      add(token, message, mode, taskId = "") {
        const key = tokenKey(token, mode);
        errors.set(key, {token, message, taskId, mode});
        render();
      },
      remove,
      count: () => errors.size,
      clear() {
        errors.clear(); render();
      },
    };
  }

  function create({input, kind, request, canLookup, status, onResolved, onFailed, onStateChange,
                   pendingContainer, scope, idleLookupMs = 0}) {
    const field = document.getElementById(input);
    const progress = document.getElementById(status);
    let queue = [], busy = false, generation = 0, idleTimer = 0, composing = false;
    const pending = new Map(), resolved = new Map();
    const pendingId = pendingContainer || field.dataset?.pendingContainer;
    const pendingHost = pendingId ? document.getElementById(pendingId) : null;
    if (pendingHost) field.setAttribute("aria-describedby", [...new Set([
      ...(field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean), pendingId,
    ])].join(" "));

    function state() {
      return {pending: [...pending.values()].map(({token, mode}) => ({token, mode})), pendingCount: pending.size,
        draft: field.value, composing, blocked: !!pending.size || !!field.value.trim() || composing};
    }
    function notify() {
      if (pendingHost) pendingHost.replaceChildren(...[...pending].map(([key, row]) => {
        const item = document.createElement("div"), token = document.createElement("span"), label = document.createElement("span");
        item.className = "patient-lookup-pending";token.className = "patient-lookup-token";label.className = "patient-lookup-state";
        token.textContent = row.token;label.textContent = "查核中";
        const remove = document.createElement("button");remove.type = "button";remove.className = "quiet patient-lookup-remove";
        remove.textContent = "移除";remove.setAttribute("aria-label", "移除查核 " + row.token);
        remove.addEventListener("click", () => {
          if (pending.get(key) !== row) return;
          pending.delete(key);queue = queue.filter(value => value !== row);notify();
        });
        item.append(token, label, remove);return item;
      }));
      onStateChange?.(state());
    }

    function enqueue(values, mode = kind()) {
      for (const raw of values) {
        const token = normalize(raw);
        if (!token) continue;
        if (token.length > 32) {onFailed(token, "識別號碼最多 32 字。", mode); continue;}
        const key = tokenKey(token, mode);
        if (pending.has(key) || resolved.has(key)) continue;
        const row = {token, mode, key};
        pending.set(key, row);
        queue.push(row);
      }
      notify();
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
      const revision = generation, context = scope?.(), mode = queue[0].mode, batch = [];
      const current = () => revision === generation && (!scope || context === scope());
      while (queue.length && queue[0].mode === mode && batch.length < 100) batch.push(queue.shift());
      const activeRow = key => batch.find(row => row.key === key && pending.get(key) === row);
      const received = new Set();
      let taskId = "", delivered = 0;
      JobProgress.pending(progress, "病人基本資料", "正在核對 " + batch.length + " 位病人…");
      try {
        if (!canLookup()) throw new Error("此帳號目前無法查詢，請連線後重試。");
        const started = await request("/tasks/start", {
          kind:"resolve", identifier_kind:mode, identifiers:batch.map(row => row.token).join("\n"),
        });
        taskId = started.task_id;
        if (!current()) return;
        for (;;) {
          await JobProgress.ready();
          if (!current()) return;
          let task = await request("/tasks/detail?summary=1&id=" + encodeURIComponent(started.task_id));
          if (!current()) return;
          if (task.id && task.id !== taskId) throw new Error("病人查核任務範圍不符，請重新查核。");
          if (!["queued","running","cancelling"].includes(task.status) || task.done > delivered) {
            await JobProgress.ready();
            if (!current()) return;
            task = await request("/tasks/detail?id=" + encodeURIComponent(started.task_id));
            delivered = task.done || 0;
          }
          if (!current()) return;
          if (task.id && task.id !== taskId) throw new Error("病人查核任務範圍不符，請重新查核。");
          JobProgress.show(progress, task);
          for (const item of task.items || []) {
            const key = tokenKey(item.input || item.mrn, mode);
            if (item.status === "resolved" && !received.has(key) && activeRow(key)) {
              received.add(key);
              pending.delete(key);resolved.set(key, item.mrn);onResolved(item, mode);notify();
            }
          }
          if (!["queued","running","cancelling"].includes(task.status)) {
            for (const item of task.items || []) {
              const key = tokenKey(item.input || item.mrn, mode);
              if (item.status !== "resolved" && activeRow(key)) {
                received.add(key);
                onFailed(item.input || item.mrn, item.message || task.message || "病人資料未取得。", mode, taskId);
              }
            }
            for (const row of batch) if (pending.get(row.key) === row && !received.has(row.key)) onFailed(row.token, task.message || "病人資料未取得。", mode, taskId);
            if (task.status !== "completed") {
              const terminal = progress.firstElementChild;
              setTimeout(() => {if (progress.firstElementChild === terminal) progress.replaceChildren();}, 15000);
            }
            break;
          }
          await JobProgress.wait();
        }
      } catch (error) {
        if (current()) {
          progress.replaceChildren();
          for (const row of batch) if (pending.get(row.key) === row && !received.has(row.key)) onFailed(row.token, error.message || "查詢失敗。", mode, taskId);
        }
      } finally {
        if (revision === generation) {
          for (const row of batch) if (pending.get(row.key) === row) pending.delete(row.key);
          busy = false;
          notify();
          if (queue.length) void pump();
        }
      }
    }

    field.addEventListener("compositionstart", () => {composing = true; clearTimeout(idleTimer);notify();});
    field.addEventListener("input", event => {if (!event.isComposing && !composing) {consume(); scheduleIdle();}notify();});
    field.addEventListener("compositionend", () => {composing = false; consume(); scheduleIdle();notify();});
    field.addEventListener("keydown", event => {
      if (event.key !== "Enter" || event.isComposing || event.keyCode === 229 || composing) return;
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
      state,
      hasBlockers: () => state().blocked,
      forget(mrn) {for (const [key, value] of resolved) if (value === mrn) resolved.delete(key);},
      reset() {
        generation++;
        clearTimeout(idleTimer);
        composing = false;
        queue = [];
        pending.clear();
        resolved.clear();
        busy = false;
        field.value = "";
        progress.replaceChildren();
        notify();
      },
      focus: () => field.focus(),
    };
  }

  return {create, createErrors};
})();
