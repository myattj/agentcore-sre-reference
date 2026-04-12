/**
 * `/ops/*` layout — operator dashboard shell.
 *
 * Minimal chrome (header + logout form). Access control lives on each
 * child page via `requireOpsSession()` rather than here, mirroring the
 * per-page auth pattern used by `/workspace/[tenantId]/*`. This keeps
 * the login flow unguarded while everything else is locked down.
 *
 * The `/ops/login` page is intentionally reachable without a session —
 * a layout-level guard would force a redirect loop on first visit.
 */
import Link from "next/link";

export default function OpsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-1 flex-col">
      <header className="border-b border-[color:var(--border)] bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-6">
            <Link href="/ops" className="font-semibold tracking-tight">
              agent-core ops
            </Link>
            <span className="rounded bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-800">
              operator
            </span>
          </div>
          <form method="POST" action="/ops/logout">
            <button
              type="submit"
              className="text-xs text-[color:var(--muted)] hover:text-[color:var(--fg)]"
            >
              Log out
            </button>
          </form>
        </div>
      </header>
      <div className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">
        {children}
      </div>
    </div>
  );
}
