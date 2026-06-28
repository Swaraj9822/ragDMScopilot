import { useEffect, useState } from "react";
import { api } from "../api";

export type HealthStatus = "connected" | "unavailable" | "checking";

export function useHealth(): HealthStatus {
  const [status, setStatus] = useState<HealthStatus>("checking");

  useEffect(() => {
    let mounted = true;
    const check = async () => {
      try {
        await api.health();
        if (mounted) setStatus("connected");
      } catch {
        if (mounted) setStatus("unavailable");
      }
    };
    check();
    const interval = setInterval(check, 30_000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  return status;
}
