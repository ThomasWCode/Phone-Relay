import { describe, expect, it } from "vitest";
import { bearerToken, isAuthorised, safeEqual } from "../src/auth";

const secret = "a".repeat(64);
const req = (auth?: string) => new Request("https://x/state", auth ? { headers: { Authorization: auth } } : {});

describe("auth", () => {
  it("compares tokens", async () => {
    expect(await safeEqual("abc", "abc")).toBe(true);
    expect(await safeEqual("abc", "abd")).toBe(false);
    expect(await safeEqual("abc", "abcd")).toBe(false);
  });

  it("parses bearer headers", () => {
    expect(bearerToken(req("Bearer tok"))).toBe("tok");
    expect(bearerToken(req("bearer tok"))).toBe("tok");
    expect(bearerToken(req("Basic tok"))).toBeNull();
    expect(bearerToken(req())).toBeNull();
  });

  it("accepts only the right token", async () => {
    expect(await isAuthorised(req(`Bearer ${secret}`), secret)).toBe(true);
    expect(await isAuthorised(req(`Bearer ${"b".repeat(64)}`), secret)).toBe(false);
    expect(await isAuthorised(req(), secret)).toBe(false);
  });

  it("fails closed when the secret is missing or short", async () => {
    expect(await isAuthorised(req("Bearer "), "")).toBe(false);
    expect(await isAuthorised(req("Bearer x"), undefined)).toBe(false);
    expect(await isAuthorised(req("Bearer short"), "short")).toBe(false);
  });
});
