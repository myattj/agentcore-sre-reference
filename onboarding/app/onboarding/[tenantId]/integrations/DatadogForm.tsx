/**
 * Datadog requires two independent secret headers, while an AgentCore
 * OpenAPI Gateway target accepts one credential provider. Keep the reference
 * deployment fail-closed until a trusted broker/Lambda target injects both.
 */
export function DatadogForm({
  initialConnected,
}: {
  initialConnected: boolean;
}) {
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-5">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div>
          <h3 className="mb-1 font-semibold">Datadog</h3>
          <p className="text-xs text-[color:var(--muted)]">
            Pull metrics, logs, and recent alerts during triage.
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-amber-300 bg-amber-100 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-800">
          Broker required
        </span>
      </div>
      <p className="text-xs text-amber-900">
        Disabled in this reference deployment. Datadog needs both an API key
        and an Application key, so enable it only after adding a trusted
        two-secret credential broker. Raw secondary keys are never stored in
        tenant configuration.
      </p>
      {initialConnected && (
        <p className="mt-2 text-xs font-medium text-amber-900">
          A legacy Datadog connection is recorded, but this UI will not send
          or replace credentials until the broker is configured.
        </p>
      )}
    </div>
  );
}
