"use client";

import IdentityNav from "@/components/identity/identity-nav";
import DomainsPanel from "@/components/identity/domains-panel";
import { Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { EnterpriseDomain, SSOConnection } from "@/lib/identity-types";

export default function DomainsPage() {
  const domains = useLoader<EnterpriseDomain[]>(() => identityApi.domains());
  const connections = useLoader<SSOConnection[]>(() => identityApi.ssoConnections());

  return (
    <>
      <header className="page-head">
        <h1>Domains</h1>
        <p className="muted">
          Prove ownership of a domain with DNS, then decide whether it changes
          how people sign in. Until a domain is verified it imposes nothing.
        </p>
      </header>

      <IdentityNav />

      {domains.error ? <div className="error-banner">{domains.error}</div> : null}

      {domains.data ? (
        <DomainsPanel
          domains={domains.data}
          connections={connections.data ?? []}
          onChanged={() => domains.reload()}
        />
      ) : (
        <Section title="Enterprise domains">
          <p className="muted">Loading…</p>
        </Section>
      )}
    </>
  );
}
