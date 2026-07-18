"use client";

import { useEffect } from "react";

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Dashboard page failed", error);
  }, [error]);

  return (
    <main className="grid min-h-screen place-items-center bg-[color:var(--background)] px-6">
      <div className="max-w-md text-center">
        <h1 className="text-2xl font-semibold text-[color:var(--foreground)]">
          Dashboard temporarily unavailable
        </h1>
        <p className="mt-3 text-sm text-[color:var(--muted)]">
          The dashboard service could not be reached. The link may still be valid.
        </p>
        <button
          type="button"
          onClick={reset}
          className="mt-6 rounded-md bg-[color:var(--accent)] px-4 py-2 text-sm font-medium text-white hover:bg-[color:var(--accent-hover)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--accent)]"
        >
          Try again
        </button>
      </div>
    </main>
  );
}
