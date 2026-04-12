/**
 * `/ops/login` — shared-secret form for operator access.
 *
 * Pure server component: renders a standard HTML form that POSTs to
 * `/ops/login/route.ts` (Route Handler). No client-side JS, no React
 * state. The handler sets the cookie and redirects to `/ops`.
 *
 * `searchParams` is a Promise in Next.js 16 (gotcha #16). The `?e=1`
 * marker is set by the route handler on an invalid-secret POST so we
 * can show an error message without leaking the comparison result.
 */
export default async function OpsLoginPage({
  searchParams,
}: {
  searchParams: Promise<{ e?: string }>;
}) {
  const { e } = await searchParams;
  const showError = e === "1";

  return (
    <div className="mx-auto max-w-sm pt-16">
      <h1 className="mb-2 text-xl font-semibold">Operator login</h1>
      <p className="mb-6 text-sm text-[color:var(--muted)]">
        Cross-tenant metrics are gated behind a shared secret. Paste
        it below.
      </p>
      <form method="POST" action="/ops/login/submit" className="space-y-4">
        <input
          type="password"
          name="secret"
          placeholder="Admin secret"
          autoComplete="off"
          required
          className="w-full rounded-md border border-[color:var(--border)] bg-white px-3 py-2 text-sm"
        />
        <button
          type="submit"
          className="w-full rounded-md bg-[color:var(--accent)] px-3 py-2 text-sm font-semibold text-white"
        >
          Continue
        </button>
      </form>
      {showError ? (
        <p className="mt-4 text-sm text-red-700">Invalid secret.</p>
      ) : null}
    </div>
  );
}
