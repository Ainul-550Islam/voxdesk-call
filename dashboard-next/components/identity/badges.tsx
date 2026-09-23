"use client";

import { enforcementLabel, protocolLabel, scopeLabel, statusTone } from "@/lib/identity";

/**
 * A status badge. The class comes from `statusTone` so "active" reads green and
 * "disabled" reads red everywhere in the product, without each page inventing
 * its own mapping.
 */
export function StatusBadge({
  status,
  label,
}: {
  status: string;
  label?: string;
}) {
  return (
    <span className={`badge ${statusTone(status)}`}>
      {label ?? status.replace(/_/g, " ")}
    </span>
  );
}

export function ProtocolBadge({ protocol }: { protocol: string }) {
  return <span className="tag">{protocolLabel(protocol)}</span>;
}

/**
 * A scope chip. Scopes are RBAC permission values, so they are shown verbatim
 * in a monospace face — an operator comparing a key against a log needs the
 * exact string, not a prettified one.
 */
export function ScopeChip({ scope }: { scope: string }) {
  return (
    <span className="chip" title={scopeLabel(scope)}>
      {scope}
    </span>
  );
}

export function ScopeList({ scopes }: { scopes: string[] }) {
  if (scopes.length === 0) {
    return <span className="muted">No scopes</span>;
  }
  return (
    <span className="chip-list">
      {scopes.map((scope) => (
        <ScopeChip key={scope} scope={scope} />
      ))}
    </span>
  );
}

export function EnforcementBadge({ enforcement }: { enforcement: string }) {
  const value = (enforcement || "off").toLowerCase();
  const className =
    value === "require_sso" ? "completed" : value === "warn" ? "no_answer" : "none";
  return <span className={`badge ${className}`}>{enforcementLabel(value)}</span>;
}

/** `—` for a missing value, so a table cell is never blank. */
export function Dash() {
  return <span className="muted">—</span>;
}
