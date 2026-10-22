/** v2 通用小工具：轮询 hook、格式化。刻意不做全局 store —— 每页自己拉数据。 */
import { useEffect, useRef, useState } from "react";
import { ApiError, get } from "../api";

export type Polled<T> = {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
};

/** 轮询只读接口；`ms <= 0` 表示只拉一次。 */
export function usePoll<T>(path: string, ms = 0): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    const run = async () => {
      try {
        const next = await get<T>(path);
        if (alive.current) {
          setData(next);
          setError(null);
        }
      } catch (exc) {
        if (alive.current) setError(exc as ApiError);
      } finally {
        if (alive.current) setLoading(false);
      }
    };
    void run();
    if (ms <= 0) return () => void (alive.current = false);
    const timer = window.setInterval(() => void run(), ms);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [path, ms, tick]);

  return { data, error, loading, reload: () => setTick((n) => n + 1) };
}

export function fmtDuration(seconds: number | null | undefined): string {
  const s = Math.max(0, Math.floor(seconds ?? 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分`;
  if (m > 0) return `${m} 分 ${s % 60} 秒`;
  return `${s} 秒`;
}

/** 后端时间戳有秒/毫秒两种（DB 里是秒），统一按需放大。 */
export function fmtTime(ts: number | string | null | undefined): string {
  if (ts === null || ts === undefined || ts === "") return "—";
  const num = Number(ts);
  if (!Number.isFinite(num)) return String(ts);
  const ms = num < 1e11 ? num * 1000 : num;
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

export function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
