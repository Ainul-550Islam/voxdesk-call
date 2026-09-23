"use client";

import IdentityNav from "@/components/identity/identity-nav";
import ScimPanel from "@/components/identity/scim-panel";
import { Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { SSOConnection, ScimCredential } from "@/lib/identity-types";

export default function ScimSettingsPage() {
  const credentials = useLoader<ScimCredential[]>(() => identityApi.scimCredentials());
  const connections = useLoader<SSOConnection[]>(() => identityApi.ssoConnections());
  const status = useLoader(() => identityApi.status());

  return (
    <>
      <header className="page-head">
        <h1>Provisioning</h1>
        <p className="muted">
          SCIM 2.0: your identity provider creates, updates and deactivates
          people here, with a token that can do nothing else.
        </p>
      </header>

      <IdentityNav />

      {status.data && !status.data.policy.scim_enabled ? (
        <div className="warn-banner" role="alert">
          SCIM provisioning is switched off for this workspace. The provider
          will keep receiving errors until it is switched back on.
        </div>
      ) : null}

      {credentials.error ? <div className="error-banner">{credentials.error}</div> : null}

      {credentials.data ? (
        <ScimPanel
          credentials={credentials.data}
          connections={connections.data ?? []}
          onChanged={() => credentials.reload()}
        />
      ) : (
        <Section title="SCIM provisioning">
          <p className="muted">Loading…</p>
        </Section>
      )}
    </>
  );
}
