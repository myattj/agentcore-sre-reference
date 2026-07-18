export default function DashboardNotFound() {
  return (
    <main className="grid min-h-screen place-items-center bg-[color:var(--background)] px-6">
      <div className="max-w-md text-center">
        <h1 className="text-2xl font-semibold text-[color:var(--foreground)]">
          Dashboard unavailable
        </h1>
        <p className="mt-3 text-sm text-[color:var(--muted)]">
          This dashboard link is invalid, expired, or no longer exists.
        </p>
      </div>
    </main>
  );
}
