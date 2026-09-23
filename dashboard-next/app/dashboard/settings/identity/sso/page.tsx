"use client";

import IdentityNav from "@/components/identity/identity-nav";
import SsoPanel from "@/components/identity/sso-panel";
import { Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { SSOAttempt, SSOConnection } from "@/lib/identity-types";

export default function SsoSettingsPage() {
  const connections = useLoader<SSOConnection[]>(() => identityApi.ssoConnections());
  const attempts = useLoader<SSOAttempt[]>(() => identityApi.ssoAttempts());
  const status = useLoader(() => identityApi.status());

  return (
    <>
      <header className="page-head">
        <h1>Single sign-on</h1>
        <p className="muted">
          OpenID Connect and SAML 2.0 connections to an identity provider. A
          connection affects nobody until it is activated.
        </p>
      </header>

      <IdentityNav />

      {status.data && !status.data.features.sso_enabled ? (
        <div className="warn-banner" role="alert">
          Single sign-on is switched off for this deployment, so no connection
          can be activated here.
        </div>
      ) : null}

      {connections.error ? <div className="error-banner">{connections.error}</div> : null}
      {attempts.error ? <div className="error-banner">{attempts.error}</div> : null}

      {connections.data ? (
        <SsoPanel
          connections={connections.data}
          attempts={attempts.data ?? []}
          onChanged={() => {
            connections.reload();
            attempts.reload();
          }}
        />
      ) : (
        <Section title="Single sign-on connections">
          <p className="muted">Loading…</p>
        </Section>
      )}
    </>
  );
}
