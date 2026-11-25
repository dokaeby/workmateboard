import type { ButtonHTMLAttributes, ReactNode } from "react";
import { useI18n, registerI18n } from "../i18n";

/** 공통 UI 프리미티브 — 모든 feature는 이 컴포넌트만 사용한다. */

registerI18n({
  ko: {
    "ui.loading": "로딩…",
    "ui.retry": "다시 시도",
    "common.all": "전체",
    "common.delete": "삭제",
    "common.last7": "최근 7일",
    "common.last30": "최근 30일",
    "common.last90": "최근 90일",
    "common.last365": "최근 365일",
  },
  en: {
    "ui.loading": "Loading…",
    "ui.retry": "Retry",
    "common.all": "All",
    "common.delete": "Delete",
    "common.last7": "Last 7 days",
    "common.last30": "Last 30 days",
    "common.last90": "Last 90 days",
    "common.last365": "Last 365 days",
  },
}, { core: true });

type BtnVariant = "primary" | "secondary" | "danger" | "ghost";
const btnCls: Record<BtnVariant, string> = {
  primary: "bg-accent text-white hover:bg-accent-hover disabled:opacity-50",
  secondary: "bg-surface text-ink-2 border border-border hover:bg-surface-hover disabled:opacity-50",
  danger: "bg-err text-white hover:opacity-90 disabled:opacity-50",
  ghost: "text-accent hover:bg-accent-soft disabled:opacity-50",
};

export function Button({
  variant = "primary", className = "", children, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant }) {
  return (
    <button
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${btnCls[variant]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Card({ title, actions, children, className = "" }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`rounded-xl border border-border bg-surface p-4 shadow-sm ${className}`}>
      {(title || actions) && (
        <header className="mb-3 flex items-center justify-between gap-2">
          <h2 className="text-base font-semibold text-ink">{title}</h2>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted">{children}</div>;
}

export function Spinner({ label }: { label?: string }) {
  const { t } = useI18n();
  return (
    <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted">
      <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent" />
      {label ?? t("ui.loading", "로딩…")}
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useI18n();
  return (
    <div className="rounded-lg border border-err-border bg-err-soft p-4 text-sm text-err">
      <div>{message}</div>
      {onRetry && <Button variant="secondary" className="mt-2" onClick={onRetry}>{t("ui.retry", "다시 시도")}</Button>}
    </div>
  );
}

export function Badge({ tone = "muted", children }: {
  tone?: "muted" | "ok" | "warn" | "err" | "accent"; children: ReactNode;
}) {
  const cls = {
    muted: "bg-surface-hover text-muted",
    ok: "bg-ok-soft text-ok",
    warn: "bg-warn-soft text-warn",
    err: "bg-err-soft text-err",
    accent: "bg-accent-soft text-accent",
  }[tone];
  return <span className={`inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-medium ${cls}`}>{children}</span>;
}

export function PageHead({ title, desc, children }: { title: string; desc?: string; children?: ReactNode }) {
  return (
    <div className="mb-4">
      <h1 className="text-xl font-bold text-ink">{title}</h1>
      {desc && <p className="mt-0.5 text-sm text-muted">{desc}</p>}
      {children && <div className="mt-2 flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}
