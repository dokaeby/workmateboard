import { useEffect, useState, type FormEvent } from "react";
import { apiGet, apiPost, ApiError } from "../../shared/api/client";
import { useI18n } from "../../shared/i18n";

/** 미인증 화면 — Google OAuth + 이메일 회원가입/로그인 (세션 쿠키 인증).
 *  X로 닫으면 앱 셸은 유지하고 하단 배너로 재로그인을 유도한다(레거시 auth-close 패리티). */
export function LoginOverlay({ authError, onClose }: { authError?: string | null; onClose: () => void }) {
  const { t } = useI18n();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");
  const [needVerify, setNeedVerify] = useState(false);
  const [googleEnabled, setGoogleEnabled] = useState(false);

  useEffect(() => {
    apiGet<{ google_enabled?: boolean }>("/auth/config")
      .then((d) => setGoogleEnabled(d.google_enabled === true))
      .catch(() => setGoogleEnabled(false));
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !email.trim() || !password) return;
    setBusy(true);
    setErr("");
    setNotice("");
    setNeedVerify(false);
    try {
      if (mode === "login") {
        await apiPost("/auth/login", { email: email.trim(), password });
        window.location.reload();
        return;
      }
      const res = await apiPost<{ email_verification_pending?: boolean; verify_email_sent?: boolean }>(
        "/auth/signup",
        { email: email.trim(), password, display_name: name.trim(), agree_terms: agree },
      );
      if (res?.email_verification_pending) {
        // 세션 미발급 — 인증 메일 확인 후 로그인해야 한다.
        // 메일 발송 실패(verify_email_sent=false)면 재발송 경로를 바로 연다.
        setNeedVerify(true);
        setNotice(
          res.verify_email_sent === false
            ? t("auth.verifySendFail", "인증 메일 발송에 실패했습니다 — 아래 버튼으로 재발송하세요")
            : t("auth.verifySent", "인증 메일을 보냈습니다 — 메일의 링크를 클릭한 뒤 로그인하세요"),
        );
        setMode("login");
      } else {
        window.location.reload();
      }
    } catch (e2) {
      if (e2 instanceof ApiError && e2.code === "email_not_verified") setNeedVerify(true);
      setErr(e2 instanceof Error ? e2.message : String(e2));
    } finally {
      setBusy(false);
    }
  };

  const resendVerify = async () => {
    try {
      const res = await apiPost<{ sent?: boolean }>("/auth/verify/resend", { email: email.trim() });
      setNotice(
        res?.sent === false
          ? t("auth.resendNone", "발송되지 않았습니다 — 이미 인증됐거나 가입된 이메일이 아닐 수 있습니다")
          : t("auth.resendDone", "인증 메일을 재발송했습니다"),
      );
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    }
  };

  const inputCls =
    "w-full rounded-[10px] border border-border bg-bg px-3 py-2 text-sm text-ink outline-none focus:border-accent";

  return (
    <div className="flex h-dvh items-center justify-center bg-bg p-4">
      <div className="relative w-full max-w-sm rounded-2xl border border-border bg-surface p-8 text-center shadow-sm">
        <button
          type="button"
          onClick={onClose}
          title={t("auth.close", "닫기")}
          aria-label={t("auth.close", "닫기")}
          className="absolute right-3 top-3 border-none bg-transparent px-1.5 text-lg leading-none text-muted hover:text-ink"
        >
          ✕
        </button>
        <div className="mb-1 text-xl font-bold text-accent">jobMate</div>
        <p className="mb-6 text-sm text-muted">
          {t("auth.required", "로그인이 필요합니다 — 세션이 만료됐거나 로그인이 필요해요.")}
        </p>
        {authError && (
          <p className="mb-4 rounded-lg border border-err-border bg-err-soft px-3 py-2 text-xs text-err">
            {t(`auth.err.${authError}`, authError)}
          </p>
        )}
        {googleEnabled && (
          <a
            href="/api/v1/auth/google"
            className="mb-4 block rounded-[10px] border border-border-strong bg-surface px-4 py-2.5 font-semibold text-ink no-underline transition-colors hover:bg-surface-hover"
          >
            {t("auth.google", "Google로 로그인")}
          </a>
        )}
        <form onSubmit={submit} className="text-left">
          {mode === "signup" && (
            <>
              <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-name">
                {t("auth.displayName", "표시 이름 (선택)")}
              </label>
              <input
                id="jm-name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="nickname"
                className={inputCls + " mb-3"}
              />
            </>
          )}
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-email">
            {t("auth.email", "이메일")}
          </label>
          <input
            id="jm-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            className={inputCls + " mb-3"}
          />
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="jm-password">
            {t("auth.password", "비밀번호")}
          </label>
          <input
            id="jm-password"
            type="password"
            required
            minLength={mode === "signup" ? 8 : undefined}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
            className={inputCls + " mb-3"}
          />
          {mode === "signup" && (
            <label className="mb-3 flex items-start gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={agree}
                onChange={(e) => setAgree(e.target.checked)}
                className="mt-0.5"
              />
              <span>{t("auth.agreeTerms", "이용약관 및 개인정보처리방침에 동의합니다")}</span>
            </label>
          )}
          {err && <p className="mb-2 text-xs text-err">{err}</p>}
          {notice && <p className="mb-2 text-xs text-accent">{notice}</p>}
          {needVerify && (
            <button
              type="button"
              onClick={resendVerify}
              className="mb-2 text-xs font-medium text-accent underline"
            >
              {t("auth.resendVerify", "인증 메일 재발송")}
            </button>
          )}
          <button
            type="submit"
            disabled={busy || !email.trim() || !password || (mode === "signup" && !agree)}
            className="w-full rounded-[10px] border border-border-strong bg-accent px-4 py-2.5 font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy
              ? t("common.loading", "로딩…")
              : mode === "login"
                ? t("auth.login", "로그인")
                : t("auth.signup", "회원가입")}
          </button>
        </form>
        <button
          type="button"
          onClick={() => { setMode(mode === "login" ? "signup" : "login"); setErr(""); setNotice(""); setNeedVerify(false); }}
          className="mt-4 text-xs text-muted underline"
        >
          {mode === "login"
            ? t("auth.toSignup", "계정이 없나요? 회원가입")
            : t("auth.toLogin", "이미 계정이 있나요? 로그인")}
        </button>
      </div>
    </div>
  );
}
