import { describe, it, expect } from "vitest";
import { fmtAge, fmtElapsed, usd, usdFromMicro, usdPreferMicro, jsonSummary } from "./format";
import { setLang } from "../i18n";

describe("fmtAge", () => {
  it("handles invalid/negative", () => {
    expect(fmtAge(undefined)).toBe("-");
    expect(fmtAge(-5)).toBe("-");
    expect(fmtAge(NaN)).toBe("-");
  });
  it("ko relative units", () => {
    setLang("ko");
    expect(fmtAge(30)).toBe("방금 전");
    expect(fmtAge(120)).toBe("2분 전");
    expect(fmtAge(7200)).toBe("2시간 전");
    expect(fmtAge(200000)).toBe("2일 전");
  });
  it("en relative units", () => {
    setLang("en");
    expect(fmtAge(30)).toBe("just now");
    expect(fmtAge(120)).toBe("2m ago");
    expect(fmtAge(7200)).toBe("2h ago");
    expect(fmtAge(200000)).toBe("2d ago");
    setLang("ko");
  });
});

describe("fmtElapsed", () => {
  it("invalid input", () => {
    expect(fmtElapsed(null)).toBe("-");
    expect(fmtElapsed("not-a-date")).toBe("-");
  });
  it("ko duration", () => {
    setLang("ko");
    const s = new Date(Date.now() - 90_000).toISOString();
    expect(fmtElapsed(s)).toBe("1분 30초");
    const e = new Date(Date.now() - 7_200_000).toISOString();
    expect(fmtElapsed(e)).toBe("2시간 0분");
  });
  it("en duration", () => {
    setLang("en");
    const s = new Date(Date.now() - 90_000).toISOString();
    expect(fmtElapsed(s)).toBe("1m 30s");
    setLang("ko");
  });
  it("explicit end", () => {
    setLang("ko");
    expect(fmtElapsed("2024-01-01T00:00:00Z", "2024-01-01T00:01:30Z")).toBe("1분 30초");
  });
});

describe("usd", () => {
  it("formats", () => {
    expect(usd(0)).toBe("$0.0000"); // n<1 → 소수 4자리
    expect(usd(1.5)).toBe("$1.50");
    expect(usd(0.00012)).toBe("$0.0001");
    expect(usd(null)).toBe("$0.0000"); // Number(null)=0 → n<1 분기
    expect(usd(NaN)).toBe("$0.00");
  });
});

describe("usdFromMicro", () => {
  it("converts integer microusd to currency string", () => {
    setLang("en");
    expect(usdFromMicro(1_500_000)).toContain("1.50");
    expect(usdFromMicro(0)).toContain("0.00");
    expect(usdFromMicro(123)).toContain("0.0001"); // 0.000123 USD → 4자리
    expect(usdFromMicro(null)).toBe(usd(null));
    expect(usdFromMicro(NaN)).toBe(usd(null));
    setLang("ko");
  });
});

describe("usdPreferMicro", () => {
  it("prefers microusd when present, falls back to usd float", () => {
    setLang("en");
    expect(usdPreferMicro(9.99, 2_000_000)).toContain("2.00");
    expect(usdPreferMicro(1.5, null)).toBe(usd(1.5));
    setLang("ko");
  });
});

describe("jsonSummary", () => {
  it("truncates", () => {
    expect(jsonSummary({ a: 1 })).toBe('{"a":1}');
    expect(jsonSummary("x".repeat(400), 10)).toHaveLength(11); // 10 + …
  });
  it("circular fallback", () => {
    const o: Record<string, unknown> = {};
    o.self = o;
    expect(jsonSummary(o)).toBe("[object Object]");
  });
});
