"use client";

import IdentityNav from "@/components/identity/identity-nav";
import ServiceAccountsPanel from "@/components/identity/service-accounts-panel";
import { Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { ScopeCatalogue, ServiceAccount } from "@/lib/identity-types";

export default function ServiceAccountsPage() {
  const accounts = useLoader<ServiceAccount[]>(() => identityApi.serviceAccounts());
  const catalogue = useLoader<ScopeCatalogue>(() => identityApi.apiKeyScopes());
  const status = useLoader(() => identityApi.status());

  return (
    <>
      <header className="page-head">
        <h1>Service accounts</h1>
        <p className="muted">
          Machine identities for daemons and integrations: their own scopes,
          their own credentials, an owner, and a switch an operator can throw.
        </p>
      </header>

      <IdentityNav />

      {status.data && !status.data.policy.service_accounts_allowed ? (
        <div className="warn-banner" role="alert">
          Service accounts are switched off for this workspace, so existing
          credentials are refused on every request until that changes.
        </div>
      ) : null}

      {accounts.error ? <div className="error-banner">{accounts.error}</div> : null}
      {catalogue.error ? <div className="error-banner">{catalogue.error}</div> : null}

      {accounts.data ? (
        <ServiceAccountsPanel
          accounts={accounts.data}
          catalogue={catalogue.data}
          onChanged={() => accounts.reload()}
        />
      ) : (
        <Section title="Service accounts">
          <p className="muted">Loading…</p>
        </Section>
      )}
    </>
  );
}
