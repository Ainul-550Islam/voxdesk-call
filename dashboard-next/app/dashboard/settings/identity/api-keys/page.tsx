"use client";

import IdentityNav from "@/components/identity/identity-nav";
import ApiKeysPanel from "@/components/identity/api-keys-panel";
import { CheckField, Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import { useState } from "react";
import type { ApiKey, ScopeCatalogue } from "@/lib/identity-types";

export default function ApiKeysPage() {
  const [includeAll, setIncludeAll] = useState(true);
  const keys = useLoader<ApiKey[]>(
    () => identityApi.apiKeys({ includeRevoked: includeAll }),
    { deps: [includeAll] },
  );
  const catalogue = useLoader<ScopeCatalogue>(() => identityApi.apiKeyScopes());

  return (
    <>
      <header className="page-head">
        <h1>API keys</h1>
        <p className="muted">
          Secrets a script or an integration authenticates with. Stored as
          hashes: a key can be rotated or revoked, never read back.
        </p>
      </header>

      <IdentityNav />

      <Section
        title="Scope of the list"
        description="By default a key list shows every key in this workspace, which is how a leaked one gets found."
      >
        <CheckField
          id="mine-only"
          label="Show only my keys"
          checked={!includeAll}
          onChange={(value) => setIncludeAll(!value)}
          hint="Every key you can see here belongs to this workspace; keys never cross tenants."
        />
      </Section>

      {keys.error ? <div className="error-banner">{keys.error}</div> : null}
      {catalogue.error ? <div className="error-banner">{catalogue.error}</div> : null}

      {keys.data ? (
        <ApiKeysPanel
          keys={keys.data}
          catalogue={catalogue.data}
          onChanged={() => keys.reload()}
        />
      ) : (
        <Section title="API keys">
          <p className="muted">Loading…</p>
        </Section>
      )}
    </>
  );
}
