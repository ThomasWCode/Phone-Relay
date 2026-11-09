import { describe, expect, it } from "vitest";
import { evaluateCheck, fullComponent, type CheckInputs, type StepOutcome } from "../src/check";

const PKG = "com.example.app";
const FOCUS = `${PKG}/${PKG}.block.FocusService`;
const ok = (result: Record<string, unknown>): StepOutcome => ({ ok: true, result });
const crashLines = [
  "--------- beginning of crash",
  "11-09 07:00:00.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION: main",
  `11-09 07:00:00.000  1234  1234 E AndroidRuntime: Process: ${PKG}, PID: 1234`,
  "11-09 07:00:00.000  1234  1234 E AndroidRuntime: java.lang.IllegalStateException: boom",
  "11-09 07:01:00.000  2345  2345 E AndroidRuntime: FATAL EXCEPTION: main",
  "11-09 07:01:00.000  2345  2345 E AndroidRuntime: Process: com.other.app, PID: 2345",
  "11-09 07:02:00.000  3456  3456 E AndroidRuntime: FATAL EXCEPTION: main",
  `11-09 07:02:00.000  3456  3456 E AndroidRuntime: Process: ${PKG}2, PID: 3456`,
  "11-09 07:03:00.000  4567  4567 E AndroidRuntime: FATAL EXCEPTION: worker",
  `11-09 07:03:00.000  4567  4567 E AndroidRuntime: Process: ${PKG}:sync, PID: 4567`,
];

function inputs(over: Partial<CheckInputs> = {}): CheckInputs {
  return {
    package: ok({ name: PKG, version_name: "1.2.0", version_code: 12, last_update_time: "2026-11-09 06:58:42", debuggable: true }),
    state: ok({ foreground: "com.android.chrome" }),
    accessibility: ok({ enabled: [FOCUS, "com.b/com.b.S"], bound: [FOCUS, "com.b/com.b.S"], missing: [] }),
    crashLog: ok({ lines: ["--------- beginning of crash"], truncated: false }),
    files: ok({ entries: [{ name: "plan.json", type: "file", size: 120 }, { name: "crashes", type: "dir", size: 4096 }] }),
    ...over,
  };
}

describe("evaluateCheck", () => {
  it("all well: the version, what is in front, the services bound, no crash, the files' sizes", () => {
    const { report, record } = evaluateCheck({ package: PKG, services: [`${PKG}/.block.FocusService`] }, inputs(), null);
    expect(report.ok).toBe(true);
    expect(report.problems).toEqual([]);
    expect(report.foreground).toBe("com.android.chrome");
    expect((report.accessibility as { services: unknown }).services).toEqual([{ component: FOCUS, enabled: true, bound: true }]);
    expect(report.crashes).toEqual({ count: 0, since: "2026-11-09 06:58:42", first: [] });
    expect(report.files).toEqual({ entries: [{ name: "plan.json", size: 120, was: null }], gone: [] });
    expect(record).toEqual({ enabled: [FOCUS, "com.b/com.b.S"], files: { "plan.json": 120 } });
  });

  it("a crash of the package since the install, not another app's", () => {
    const { report } = evaluateCheck({ package: PKG }, inputs({ crashLog: ok({ lines: crashLines }) }), null);
    expect(report.ok).toBe(false);
    const crashes = report.crashes as { count: number; first: string[] };
    expect(crashes.count).toBe(2); // its own :sync process too, not com.example.app2's
    expect(crashes.first[0]).toContain("FATAL EXCEPTION");
    expect(report.problems).toEqual(["2 crashes in the crash log since the install"]);
  });

  it("a service enabled but not bound, a named one not enabled, and a dump that names none", () => {
    const a = ok({ enabled: [FOCUS, "com.b/com.b.S"], bound: ["com.b/com.b.S"], missing: [FOCUS] });
    let { report } = evaluateCheck({ package: PKG, services: ["com.c/.X"] }, inputs({ accessibility: a }), null);
    expect(report.problems).toEqual([`accessibility service enabled but not bound: ${FOCUS}`, "accessibility service not enabled: com.c/com.c.X"]);
    ({ report } = evaluateCheck({ package: PKG }, inputs({ accessibility: ok({ enabled: [FOCUS], bound: null, missing: null }) }), null));
    expect(report.ok).toBe(false);
    expect(String((report.problems as string[])[0])).toMatch(/could not be read/);
  });

  it("what has gone since the last check is told, and the record kept until accept_gone", () => {
    const last = { enabled: [FOCUS, "com.b/com.b.S"], files: { "plan.json": 100, "settings.json": 50 } };
    const now = inputs({ accessibility: ok({ enabled: ["com.b/com.b.S"], bound: ["com.b/com.b.S"], missing: [] }) });
    const { report, record } = evaluateCheck({ package: PKG }, now, last);
    expect(report.ok).toBe(false);
    expect(report.problems).toEqual([
      `accessibility service gone from the enabled list since the last check: ${FOCUS}`,
      "file gone since the last check: files/settings.json",
    ]);
    expect((report.files as { entries: unknown }).entries).toEqual([{ name: "plan.json", size: 120, was: 100 }]);
    expect(record).toEqual(last); // kept as evidence
    const accepted = evaluateCheck({ package: PKG, accept_gone: true }, now, last);
    expect(accepted.record).toEqual({ enabled: ["com.b/com.b.S"], files: { "plan.json": 120 } });
  });

  it("a check that could not run is not a clean one", () => {
    const failed: StepOutcome = { ok: false, error: "shell_unavailable", detail: "wireless debugging is off" };
    const { report, record } = evaluateCheck({ package: PKG }, inputs({ accessibility: failed, crashLog: failed, files: null }), { enabled: [FOCUS] });
    expect(report.ok).toBe(false);
    expect(report.problems).toEqual([
      "the accessibility services could not be read (shell_unavailable: wireless debugging is off)",
      "the crash log could not be read (shell_unavailable: wireless debugging is off)",
    ]);
    expect(record).toEqual({ enabled: [FOCUS] }); // nothing read, nothing replaced
    const noState = evaluateCheck({ package: PKG }, inputs({ state: failed }), null);
    expect(noState.report.ok).toBe(false);
    expect(noState.report.problems).toEqual(["what is in front could not be read (shell_unavailable: wireless debugging is off)"]);
    const missing = evaluateCheck({ package: PKG }, inputs({ package: { ok: false, error: "not_found" }, crashLog: null, files: null }), null);
    expect(missing.report.ok).toBe(false);
    expect(missing.report.package).toBeNull();
  });

  it("fullComponent expands a short class name only", () => {
    expect(fullComponent("com.a/.B")).toBe("com.a/com.a.B");
    expect(fullComponent("com.a/com.x.B")).toBe("com.a/com.x.B");
  });
});
