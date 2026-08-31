import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { PageController, PageControllerName } from "../controller-elements";

const registrationEvent = "quantdesk:page-controller-registered";

export function PageControllerPanel({ active, name }: { active: boolean; name: PageControllerName }) {
  const activeRef = useRef(active);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const controllerRef = useRef<PageController | null>(null);
  const [state, setState] = useState<"error" | "loading" | "ready">(
    () => window.quantdeskHasPageController?.(name) ? "ready" : "loading",
  );

  activeRef.current = active;

  useLayoutEffect(() => {
    let mounted = true;
    let timeoutId = 0;
    const host = hostRef.current;

    const mount = () => {
      if (!mounted || !host || !window.quantdeskHasPageController?.(name)) return;
      try {
        const controller = window.quantdeskMountPageController(name, host);
        controllerRef.current = controller;
        setState("ready");
        if (activeRef.current) controller.start?.();
        else controller.pause?.();
      } catch {
        setState("error");
      }
    };

    const handleRegistration = (event: Event) => {
      const registeredName = (event as CustomEvent<{ name?: string }>).detail?.name;
      if (registeredName !== name) return;
      window.clearTimeout(timeoutId);
      mount();
    };

    if (window.quantdeskHasPageController?.(name)) mount();
    else {
      setState("loading");
      window.addEventListener(registrationEvent, handleRegistration);
      timeoutId = window.setTimeout(() => { if (mounted) setState("error"); }, 5000);
    }

    return () => {
      mounted = false;
      window.clearTimeout(timeoutId);
      window.removeEventListener(registrationEvent, handleRegistration);
      controllerRef.current?.pause?.();
      controllerRef.current = null;
      if (host) window.quantdeskUnmountPageController(host);
    };
  }, [name]);

  useEffect(() => {
    if (state !== "ready") return;
    if (active) controllerRef.current?.start?.();
    else controllerRef.current?.pause?.();
  }, [active, state]);

  return <>
    {active && state !== "ready" ? <div className={`controller-panel-state ${state}`} role="status">
      <span aria-hidden="true">{state === "error" ? "!" : "·"}</span>
      <div><strong>{state === "error" ? "功能组件加载失败" : "正在加载功能组件"}</strong><small>{state === "error" ? "请刷新页面；若仍失败，请检查后端静态资源服务。" : "正在连接原版界面与数据服务…"}</small></div>
    </div> : null}
    <div ref={hostRef} className="page-controller-host" data-controller={name} />
  </>;
}
