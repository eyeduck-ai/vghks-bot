"use strict";
// Short single-choice fields share native radio semantics and keyboard support.
window.Choices = (() => {
  const controls = new WeakMap();
  const tracks = new WeakSet();
  let sequence = 0;
  function position(options) {
    const label = options.querySelector("input:checked")?.parentElement;
    const ready = label && options.getBoundingClientRect().width > 0;
    options.classList.toggle("has-indicator", !!ready);
    if (!ready) return;
    for (const [key, value] of Object.entries({x:label.offsetLeft,y:label.offsetTop,w:label.offsetWidth,h:label.offsetHeight}))
      options.style.setProperty("--choice-"+key, value+"px");
  }
  const resize = new ResizeObserver(entries => {
    for (const {target} of entries) {if (target.isConnected) position(target);else resize.unobserve(target);}
  });
  function track(options) {
    if (!tracks.has(options)) {
      tracks.add(options);resize.observe(options);
      options.addEventListener("change", () => position(options));
    }
    position(options);
  }
  function enhance(select) {
    if (controls.has(select)) return controls.get(select).sync();
    const label = select.closest("label");
    if (!label) return;
    const field = document.createElement("fieldset"), legend = document.createElement("legend"), options = document.createElement("div");
    field.className = "choice-field " + label.className;
    field.hidden = label.hidden;
    if (label.id) field.id = label.id;
    legend.textContent = [...label.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join("").trim();
    legend.id = "choice-label-" + (++sequence);
    select.setAttribute("aria-labelledby", legend.id);
    select.dataset.choices = "";
    options.className = "choice-options";
    label.replaceWith(field);
    field.append(legend, select, options);
    let signature = "";
    const sync = () => {
      const items = [...select.options], short = items.length > 0 && items.length <= 5;
      select.hidden = short;
      options.hidden = !short;
      field.disabled = select.disabled;
      const next = JSON.stringify(items.map(o => [o.value, o.text, o.disabled]));
      if (next !== signature) {
        signature = next;
        options.replaceChildren();
        if (short) for (const option of items) {
          const item = document.createElement("label"), radio = document.createElement("input"), text = document.createElement("span");
          radio.type = "radio";
          radio.name = legend.id;
          radio.value = option.value;
          radio.disabled = option.disabled;
          if (select.hasAttribute("aria-describedby")) radio.setAttribute("aria-describedby", select.getAttribute("aria-describedby"));
          radio.addEventListener("change", () => {
            if (!radio.checked) return;
            select.value = radio.value;
            sync();
            select.dispatchEvent(new Event("input", {bubbles:true}));
            select.dispatchEvent(new Event("change", {bubbles:true}));
          });
          text.textContent = option.text;
          item.append(radio, text);
          options.append(item);
        }
      }
      for (const radio of options.querySelectorAll("input")) radio.checked = radio.value === select.value;
      track(options);
    };
    controls.set(select, {sync});
    select.addEventListener("change", sync);
    new MutationObserver(sync).observe(select, {childList:true, subtree:true, attributes:true, attributeFilter:["disabled"]});
    sync();
  }
  function sync(root=document) {
    for (const select of root.querySelectorAll("select[data-choices]")) enhance(select);
    for (const options of root.querySelectorAll(".choice-options")) track(options);
  }
  sync();
  return {enhance, sync};
})();
