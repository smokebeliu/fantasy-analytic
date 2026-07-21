"use client";

export function LoadingState({ label = "Загрузка данных…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite" data-testid="loading">
      <div className="spinner" />
      <p>{label}</p>
    </div>
  );
}

export function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="panel--pad" data-testid="skeleton">
      {Array.from({ length: rows }).map((_, i) => (
        <div className="skeleton-row" key={i} />
      ))}
    </div>
  );
}

export function EmptyState({
  title = "Ничего не найдено",
  hint,
}: {
  title?: string;
  hint?: string;
}) {
  return (
    <div className="state" data-testid="empty">
      <div style={{ fontSize: 34 }}>∅</div>
      <h3>{title}</h3>
      {hint && <p>{hint}</p>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="state state--error" role="alert" data-testid="error">
      <div style={{ fontSize: 34 }}>⚠️</div>
      <h3>Не удалось загрузить</h3>
      <p>{message}</p>
      {onRetry && (
        <button className="btn" onClick={onRetry} style={{ marginTop: 12 }}>
          Повторить
        </button>
      )}
    </div>
  );
}
