import { describe, expect, it } from "vitest";
import { maskSecrets } from "./redact";

describe("maskSecrets", () => {
  it("masks Bearer tokens", () => {
    expect(maskSecrets("Authorization: Bearer abcdef123456")).toBe("Authorization: Bearer ***");
  });

  it("masks key=value credentials", () => {
    expect(maskSecrets("api_key=sk-live-123456 & x=1")).toBe("api_key=*** & x=1");
    expect(maskSecrets("password: hunter2")).toBe("password=***");
  });

  it("masks sk- style tokens", () => {
    expect(maskSecrets("key sk-abc123def456ghi")).toBe("key sk-***");
  });

  it("masks JWTs", () => {
    const jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c";
    expect(maskSecrets(`token ${jwt}`)).not.toContain("eyJhbGciOiJIUzI1NiJ9");
  });

  it("leaves ordinary text untouched", () => {
    expect(maskSecrets("run completed in 3s")).toBe("run completed in 3s");
    expect(maskSecrets(null)).toBe("");
    expect(maskSecrets(undefined)).toBe("");
  });
});
