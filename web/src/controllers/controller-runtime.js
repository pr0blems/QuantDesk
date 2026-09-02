(function initializeQuantDeskControllerRuntime(global) {
  if (global.quantdeskRegisterPageController) return;

  const definitions = new Map();
  const mountedControllers = new WeakMap();
  const registrationEvent = "quantdesk:page-controller-registered";

  class QuantDeskPageController {
    constructor(host, options = {}) {
      if (!(host instanceof HTMLElement)) {
        throw new TypeError("页面控制器需要有效的 HTMLElement 宿主");
      }
      this.host = host;
      if (options.shadow && !host.shadowRoot) host.attachShadow({ mode: "open" });
    }

    get shadowRoot() { return this.host.shadowRoot; }
    get dataset() { return this.host.dataset; }
    get innerHTML() { return this.host.innerHTML; }
    set innerHTML(value) { this.host.innerHTML = value; }
    querySelector(selector) { return this.host.querySelector(selector); }
    querySelectorAll(selector) { return this.host.querySelectorAll(selector); }
  }

  function mountPageController(name, host) {
    const existing = mountedControllers.get(host);
    if (existing) return existing;
    const Controller = definitions.get(name);
    if (!Controller) throw new Error(`页面控制器尚未加载：${name}`);
    const controller = new Controller(host);
    mountedControllers.set(host, controller);
    controller.connectedCallback?.();
    return controller;
  }

  function unmountPageController(host) {
    const controller = mountedControllers.get(host);
    if (!controller) return;
    controller.disconnectedCallback?.();
    mountedControllers.delete(host);
  }

  function getMountedPageController(host) {
    return mountedControllers.get(host) || null;
  }

  function registerPageController(name, Controller) {
    if (!name || typeof Controller !== "function") {
      throw new TypeError("页面控制器注册参数无效");
    }
    definitions.set(name, Controller);

    // AI 研究弹窗仍会嵌套使用同一控制器；正式页面由 React 宿主直接挂载。
    if (!global.customElements.get(name)) {
      class QuantDeskControllerElement extends HTMLElement {
        connectedCallback() { mountPageController(name, this); }
        disconnectedCallback() { unmountPageController(this); }
        start() { mountedControllers.get(this)?.start?.(); }
        pause() { mountedControllers.get(this)?.pause?.(); }
      }
      global.customElements.define(name, QuantDeskControllerElement);
    }

    global.dispatchEvent(new CustomEvent(registrationEvent, { detail: { name } }));
  }

  global.QuantDeskPageController = QuantDeskPageController;
  global.quantdeskRegisterPageController = registerPageController;
  global.quantdeskMountPageController = mountPageController;
  global.quantdeskGetMountedPageController = getMountedPageController;
  global.quantdeskUnmountPageController = unmountPageController;
  global.quantdeskHasPageController = (name) => definitions.has(name);
  global.quantdeskPageControllerRegistrationEvent = registrationEvent;
})(window);
