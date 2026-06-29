import styles from "./common.module.css";

interface SkeletonProps {
  width?: string | number;
  height?: string | number;
  radius?: string | number;
  className?: string;
}

export function Skeleton({ width = "100%", height = 16, radius, className }: SkeletonProps) {
  return (
    <span
      className={`${styles.skeleton} ${className ?? ""}`}
      style={{
        display: "block",
        width,
        height,
        borderRadius: radius,
      }}
      aria-hidden="true"
    />
  );
}
