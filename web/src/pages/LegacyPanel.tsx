import { useEffect, useLayoutEffect, useRef, useState } from "react";

type LegacyElement = HTMLElement & {
  pause?: () => void;
  start?: () => void;
};

export function LegacyPanel({ active, tag }: { active: boolean; tag: "backtest-workbench" | "contract-monitor" | "live-dashboard" | "paper-dashboard" | "strategy-center" }) {
  const activeRef = useRef(active);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const elementRef = useRef<LegacyElement | null>(null);
  const [definitionState, setDefinitionState] = useState<"error" | "loading" | "ready">(
    () => window.customElements.get(tag) ? "ready" : "loading",
  );

  activeRef.current = active;

  useLayoutEffect(() => {
    let mounted = true;
    let timeoutId = 0;
    const host = hostRef.current;

    const mountElement = () => {
      if (!mounted || !host) return;
      const element = document.createElement(tag) as LegacyElement;
      host.replaceChildren(element);
      elementRef.current = element;
      setDefinitionState("ready");
      if (activeRef.current) element.start?.();
    };

    if (window.customElements.get(tag)) {
      mountElement();
    } else {
      setDefinitionState("loading");
      timeoutId = window.setTimeout(() => {
        if (mounted) setDefinitionState("error");
      }, 5000);
      void window.customElements.whenDefined(tag).then(() => {
        if (!mounted) return;
        window.clearTimeout(timeoutId);
        mountElement();
      });
    }

    return () => {
      mounted = false;
      window.clearTimeout(timeoutId);
      elementRef.current?.pause?.();
      elementRef.current = null;
      host?.replaceChildren();
    };
  }, [tag]);

  useEffect(() => {
    if (definitionState !== "ready") return;
    if (active) elementRef.current?.start?.();
    else elementRef.current?.pause?.();
  }, [active, definitionState]);

  return <>
    {active && definitionState !== "ready" ? <div className={`legacy-panel-state ${definitionState}`} role="status">
      <span aria-hidden="true">{definitionState === "error" ? "!" : "·"}</span>
      <div><strong>{definitionState === "error" ? "功能组件加载失败" : "正在加载功能组件"}</strong><small>{definitionState === "error" ? "请刷新页面；若仍失败，请检查后端静态资源服务。" : "正在连接原版界面与数据服务…"}</small></div>
    </div> : null}
    <div ref={hostRef} className="legacy-panel-host" />
  </>;
}
