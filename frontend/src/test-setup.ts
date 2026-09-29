import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver; Recharts' ResponsiveContainer needs one to mount.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// jsdom has no modal dialogs: open and close them as a browser does, minus the top layer.
HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) {
  this.setAttribute("open", "");
};
HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) {
  this.removeAttribute("open");
};
