import { ReactNode } from "react";

interface IconProps {
  size?: number;
  className?: string;
}

const Icon = ({ children, size = 16, className = "" }: { children: ReactNode } & IconProps) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    className={className}
  >
    {children}
  </svg>
);

export const IconPlus = (p: IconProps) => (
  <Icon {...p}><path d="M12 5v14"/><path d="M5 12h14"/></Icon>
);
export const IconSearch = (p: IconProps) => (
  <Icon {...p}><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></Icon>
);
export const IconSidebar = (p: IconProps) => (
  <Icon {...p}><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></Icon>
);
export const IconSparkle = (p: IconProps) => (
  <Icon {...p}><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></Icon>
);
export const IconChevD = (p: IconProps) => (
  <Icon {...p}><path d="m6 9 6 6 6-6"/></Icon>
);
export const IconChevR = (p: IconProps) => (
  <Icon {...p}><path d="m9 6 6 6-6 6"/></Icon>
);
export const IconCopy = (p: IconProps) => (
  <Icon {...p}><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></Icon>
);
export const IconCheck = (p: IconProps) => (
  <Icon {...p}><path d="m5 12 5 5 9-12"/></Icon>
);
export const IconX = (p: IconProps) => (
  <Icon {...p}><path d="m6 6 12 12"/><path d="m18 6-12 12"/></Icon>
);
export const IconSend = (p: IconProps) => (
  <Icon {...p}><path d="m4 12 16-7-7 16-2-7z"/></Icon>
);
export const IconSun = (p: IconProps) => (
  <Icon {...p}><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/></Icon>
);
export const IconMoon = (p: IconProps) => (
  <Icon {...p}><path d="M20 14.5A8 8 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/></Icon>
);
export const IconAttach = (p: IconProps) => (
  <Icon {...p}><path d="M20 12 12.5 19.5a4.5 4.5 0 0 1-6.4-6.4l8-8a3 3 0 0 1 4.3 4.3l-8 8a1.5 1.5 0 0 1-2.1-2.1L15 9"/></Icon>
);
export const IconSlash = (p: IconProps) => (
  <Icon {...p}><path d="m17 5-10 14"/></Icon>
);
export const IconRetry = (p: IconProps) => (
  <Icon {...p}><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></Icon>
);
export const IconDots = (p: IconProps) => (
  <Icon {...p}><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></Icon>
);
export const IconBolt = (p: IconProps) => (
  <Icon {...p}><path d="M13 3 4 14h7l-1 7 9-11h-7z"/></Icon>
);
export const IconCpu = (p: IconProps) => (
  <Icon {...p}><rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></Icon>
);
export const IconTrash = (p: IconProps) => (
  <Icon {...p}><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M10 11v6M14 11v6"/></Icon>
);

export const IconLayers = (p: IconProps) => (
  <Icon {...p}><path d="M12 2 2 7l10 5 10-5z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/></Icon>
);
export const IconStop = (p: IconProps) => (
  <Icon {...p}><rect x="5" y="5" width="14" height="14" rx="1.5"/></Icon>
);

export const IconLogo = ({ size = 20 }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none">
    <rect x="2" y="4" width="20" height="16" rx="4" fill="currentColor" opacity=".12"/>
    <path
      d="M8 9 5 12l3 3M16 9l3 3-3 3M13.5 8l-3 8"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);
