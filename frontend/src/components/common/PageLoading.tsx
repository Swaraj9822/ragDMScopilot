import { Skeleton } from "./Skeleton";

export function PageLoading() {
  return (
    <div aria-busy="true" aria-label="Loading" style={{ display: "grid", gap: 16 }}>
      <Skeleton height={28} width="240px" />
      <Skeleton height={16} width="360px" />
      <Skeleton height={200} radius={10} />
    </div>
  );
}
