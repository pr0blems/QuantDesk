import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function BaseIcon({ children, ...props }: IconProps) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{children}</svg>;
}

export function OverviewIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M4 20V10h4v10M10 20V4h4v16M16 20v-7h4v7M3 20h18" /></BaseIcon>;
}

export function ApiIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="m8 5-5 7 5 7M16 5l5 7-5 7M14 3l-4 18" /></BaseIcon>;
}

export function AnalysisIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M12 3v9h9A9 9 0 1 1 12 3Z" /><path d="M15 3.7A9 9 0 0 1 20.3 9H15Z" /></BaseIcon>;
}

export function ShieldIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M12 3 4.5 6v5.5c0 4.5 3 7.6 7.5 9.5 4.5-1.9 7.5-5 7.5-9.5V6Z" /><path d="m9 12 2 2 4-4" /></BaseIcon>;
}

export function SearchIcon(props: IconProps) {
  return <BaseIcon {...props}><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></BaseIcon>;
}

export function ExportIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M12 3v12M8 7l4-4 4 4M5 13v7h14v-7" /></BaseIcon>;
}

export function CopyIcon(props: IconProps) {
  return <BaseIcon {...props}><rect x="8" y="8" width="11" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h2" /></BaseIcon>;
}

export function PlayIcon(props: IconProps) {
  return <BaseIcon {...props}><circle cx="12" cy="12" r="9" /><path d="m10 8 6 4-6 4Z" /></BaseIcon>;
}

export function ChevronIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="m9 18 6-6-6-6" /></BaseIcon>;
}
