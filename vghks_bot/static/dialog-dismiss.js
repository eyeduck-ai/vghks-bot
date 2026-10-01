"use strict";

// Backdrop clicks are retargeted to the dialog. Require the pointer to start
// and finish outside its rectangle so dragging from content does not dismiss it.
(() => {
  let outsideStart = null;
  const outside = (dialog, event) => {
    const box = dialog.getBoundingClientRect();
    return event.clientX < box.left || event.clientX > box.right ||
      event.clientY < box.top || event.clientY > box.bottom;
  };
  document.addEventListener("pointerdown", event => {
    const dialog = event.target;
    outsideStart = dialog instanceof HTMLDialogElement && dialog.open && outside(dialog, event) ? dialog : null;
  });
  document.addEventListener("click", event => {
    if (outsideStart && event.target === outsideStart && outside(outsideStart, event) && outsideStart.open) {
      outsideStart.close();
    }
    outsideStart = null;
  });
  document.addEventListener("pointercancel", () => { outsideStart = null; });
})();
