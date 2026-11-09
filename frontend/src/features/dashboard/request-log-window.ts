const MAX_WINDOW_MICROS = 7n * 24n * 60n * 60n * 1_000_000n;

function parseRequestLogTimestamp(value: string | undefined): { iso: string; micros: bigint } | undefined {
  const match = value?.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|([+-])(\d{2}):(\d{2}))?$/);
  if (!match || match[1].startsWith("0000")) return undefined;
  const local = `${match[1]}T${match[2]}:${match[3] ?? "00"}`;
  const localMillis = Date.parse(`${local}.000Z`);
  // Date.parse normalizes impossible calendar dates; never silently query one.
  if (!Number.isFinite(localMillis) || new Date(localMillis).toISOString().slice(0, 19) !== local) return undefined;
  const hours = Number(match[7] ?? 0);
  const minutes = Number(match[8] ?? 0);
  if (hours > 23 || minutes > 59) return undefined;
  const offsetMinutes = (match[6] === "-" ? -1 : 1) * (hours * 60 + minutes);
  const utc = new Date(localMillis - offsetMinutes * 60_000);
  if (utc.getUTCFullYear() < 1 || utc.getUTCFullYear() > 9999) return undefined;
  const fraction = match[4] ?? "";
  return {
    iso: `${utc.toISOString().slice(0, 19)}.${fraction.padEnd(3, "0")}Z`,
    micros: BigInt(utc.getTime()) * 1_000n + BigInt(fraction.padEnd(6, "0")),
  };
}

export function requestLogTimestamp(value: string | undefined): string | undefined {
  return parseRequestLogTimestamp(value)?.iso;
}

export function isRequestLogRangeValid(since: string | undefined, until: string | undefined): boolean {
  const start = parseRequestLogTimestamp(since);
  const end = parseRequestLogTimestamp(until);
  if (!start || !end) return false;
  const duration = end.micros - start.micros;
  return duration >= 0 && duration <= MAX_WINDOW_MICROS;
}

export function requestLogDateTimeInput(value: string | undefined): string {
  return requestLogTimestamp(value)?.slice(0, -1) ?? "";
}
