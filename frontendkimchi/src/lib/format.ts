/** Format a duration in milliseconds adaptively: "842 ms", "1.42 s", "2m 08s". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

/** Human-readable byte size. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** Shorten a trace/span id for dense display, keeping head and tail. */
export function shortenId(id: string, head = 8, tail = 4): string {
  if (id.length <= head + tail + 1) return id;
  return `${id.slice(0, head)}…${id.slice(-tail)}`;
}

export function formatAbsolute(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  });
}

export function formatRelative(iso: string, now: number = Date.now()): string {
  const date = new Date(iso);
  const diffMs = now - date.getTime();
  if (Number.isNaN(diffMs)) return iso;
  const abs = Math.abs(diffMs);
  const future = diffMs < 0;
  const sec = Math.round(abs / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return rel(sec, "second", future);
  const min = Math.round(sec / 60);
  if (min < 60) return rel(min, "minute", future);
  const hr = Math.round(min / 60);
  if (hr < 24) return rel(hr, "hour", future);
  const day = Math.round(hr / 24);
  return rel(day, "day", future);
}

function rel(value: number, unit: string, future: boolean): string {
  const plural = value === 1 ? unit : `${unit}s`;
  return future ? `in ${value} ${plural}` : `${value} ${plural} ago`;
}
