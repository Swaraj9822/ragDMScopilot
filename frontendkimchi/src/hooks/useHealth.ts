import { useQuery } from "@tanstack/react-query";
import { checkHealth } from "../api/copilot";

export type HealthState = "connected" | "unavailable" | "checking";

export function useHealth() {
  const query = useQuery({
    queryKey: ["health"],
    queryFn: checkHealth,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    retry: false,
    staleTime: 0,
  });

  const state: HealthState = query.isLoading
    ? "checking"
    : query.isSuccess && query.data?.status === "ok"
      ? "connected"
      : query.isError
        ? "unavailable"
        : "checking";

  return { state, refetch: query.refetch, isFetching: query.isFetching };
}
