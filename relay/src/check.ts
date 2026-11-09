// GET /check: the after-install checks in one call, run by the relay as several phone commands
// (package, state, accessibility, the crash log since the install, a debuggable app's files), as
// Decrastination's scripts/phone-check.sh runs them over adb. Read-only on the phone. What has gone
// since the last check (an accessibility service off the enabled list, a file) is told by comparing
// with the last check's record, kept in the relay; a record showing something gone is kept until
// `accept_gone` says that was meant, so a loss is reported on every check until then.

type Fields = Record<string, unknown>;

// One phone command's outcome, as the relay got it.
export type StepOutcome = { ok: true; result: Fields } | { ok: false; error: string; detail?: string };

// What the relay keeps between checks, per package.
export interface CheckRecord {
  enabled?: string[];
  files?: Record<string, number>;
}

export interface CheckInputs {
  package: StepOutcome;
  state: StepOutcome;
  accessibility: StepOutcome;
  // The crash buffer since the install; null when it was not read (the package is not installed).
  crashLog: StepOutcome | null;
  // The app's files/ folder; null when not read (not debuggable, or not installed).
  files: StepOutcome | null;
}

// The crash log's lines kept with the first crash of the package.
const CRASH_LINES = 20;

// pkg/.Class as pkg/pkg.Class, as both lists may name a service either way.
export function fullComponent(c: string): string {
  const [pkg, cls] = c.split("/", 2);
  return cls?.startsWith(".") ? `${pkg}/${pkg}${cls}` : c;
}

function failed(o: StepOutcome): string {
  return o.ok ? "" : `${o.error}${o.detail ? `: ${o.detail}` : ""}`;
}

function strings(v: unknown): string[] | null {
  return Array.isArray(v) && v.every((s) => typeof s === "string") ? (v as string[]) : null;
}

// The phone commands' outcomes, the last record and the agent's arguments -> the report, and the
// record to keep (null: keep the last one as it is).
export function evaluateCheck(
  args: { package: string; services?: string[]; accept_gone?: boolean },
  inputs: CheckInputs,
  last: CheckRecord | null,
): { report: Fields; record: CheckRecord } {
  const pkg = args.package;
  const problems: string[] = [];
  const record: CheckRecord = { ...(last ?? {}) };

  // The version, and when it was installed (the crash log is read from then on).
  const installed = inputs.package.ok ? inputs.package.result : null;
  if (!inputs.package.ok) problems.push(`the package could not be read (${failed(inputs.package)})`);

  const foreground = inputs.state.ok ? (inputs.state.result.foreground ?? null) : null;
  if (!inputs.state.ok) problems.push(`what is in front could not be read (${failed(inputs.state)})`);

  // Accessibility: every enabled service bound, the ones named in `services` among them, none gone.
  let accessibility: Fields | null = null;
  if (!inputs.accessibility.ok) {
    problems.push(`the accessibility services could not be read (${failed(inputs.accessibility)})`);
  } else {
    const a = inputs.accessibility.result;
    const enabled = (strings(a.enabled) ?? []).map(fullComponent);
    const bound = strings(a.bound)?.map(fullComponent) ?? null;
    const missing = strings(a.missing) ?? [];
    if (bound === null) problems.push("which accessibility services are bound could not be read (this Android's dump does not name them)");
    for (const s of missing) problems.push(`accessibility service enabled but not bound: ${s}`);
    const services = (args.services ?? []).map(fullComponent).map((component) => ({
      component,
      enabled: enabled.includes(component),
      bound: bound === null ? null : bound.includes(component),
    }));
    for (const s of services) {
      if (!s.enabled) problems.push(`accessibility service not enabled: ${s.component}`);
      else if (s.bound === false) problems.push(`accessibility service not bound: ${s.component}`);
    }
    const gone = (last?.enabled ?? []).filter((s) => !enabled.includes(s));
    for (const s of gone) problems.push(`accessibility service gone from the enabled list since the last check: ${s}`);
    if (gone.length === 0 || args.accept_gone) record.enabled = enabled;
    accessibility = { enabled, bound, missing, gone, services };
  }

  // Crashes of the package in the crash buffer since its install: FATAL EXCEPTION, then its process,
  // named exactly (its own :process ones too), so com.example.app2 is not com.example.app's.
  let crashes: Fields | null = null;
  if (inputs.crashLog !== null) {
    if (!inputs.crashLog.ok) {
      problems.push(`the crash log could not be read (${failed(inputs.crashLog)})`);
    } else {
      const lines = strings(inputs.crashLog.result.lines) ?? [];
      let count = 0;
      let first: string[] = [];
      lines.forEach((line, i) => {
        const process = /Process: ([^,\s]+)/.exec(lines[i + 1] ?? "")?.[1];
        if (line.includes("FATAL EXCEPTION") && (process === pkg || process?.startsWith(`${pkg}:`))) {
          if (count === 0) first = lines.slice(i, i + CRASH_LINES);
          count++;
        }
      });
      if (count > 0) problems.push(`${count} crash${count === 1 ? "" : "es"} in the crash log since the install`);
      crashes = { count, since: installed?.last_update_time ?? null, first };
    }
  }

  // A debuggable app's files/ folder: each file's size beside the last check's, and any gone.
  let files: Fields | null = null;
  if (inputs.files !== null) {
    if (!inputs.files.ok) {
      problems.push(`the app's files could not be listed (${failed(inputs.files)})`);
    } else {
      const entries = Array.isArray(inputs.files.result.entries) ? (inputs.files.result.entries as Fields[]) : [];
      const now: Record<string, number> = {};
      for (const e of entries) if (e.type === "file" && typeof e.name === "string") now[e.name] = Number(e.size);
      const before = last?.files ?? {};
      const gone = Object.keys(before).filter((n) => !(n in now));
      for (const n of gone) problems.push(`file gone since the last check: files/${n}`);
      if (gone.length === 0 || args.accept_gone) record.files = now;
      files = {
        entries: Object.entries(now).map(([name, size]) => ({ name, size, was: before[name] ?? null })),
        gone,
      };
    }
  }

  const report: Fields = {
    ok: problems.length === 0,
    problems,
    package: installed,
    foreground,
    accessibility,
    crashes,
    files,
  };
  return { report, record };
}
