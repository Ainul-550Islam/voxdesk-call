import Link from "next/link";

// Settings index. Kept as a server component: it is navigation and prose, with
// no state and no data of its own.
export default function SettingsPage() {
  return (
    <>
      <header className="page-head">
        <h1>Settings</h1>
        <p className="muted">Workspace configuration.</p>
      </header>

      <section className="card">
        <h2>Identity and access</h2>
        <p className="muted">
          Sign-in, second factors, sessions, single sign-on, provisioning,
          domains, API keys and service accounts.
        </p>
        <ul className="plain">
          <li>
            <Link href="/dashboard/settings/identity">Identity overview</Link> —
            workspace policy and recent identity events.
          </li>
          <li>
            <Link href="/dashboard/settings/identity/security">My security</Link> —
            your password, second factor and devices.
          </li>
          <li>
            <Link href="/dashboard/settings/identity/sso">Single sign-on</Link> —
            OIDC and SAML connections.
          </li>
          <li>
            <Link href="/dashboard/settings/identity/scim">Provisioning</Link> —
            SCIM tokens for your identity provider.
          </li>
          <li>
            <Link href="/dashboard/settings/identity/domains">Domains</Link> —
            DNS proof and enforcement.
          </li>
          <li>
            <Link href="/dashboard/settings/identity/api-keys">API keys</Link> —
            scoped tokens for scripts.
          </li>
          <li>
            <Link href="/dashboard/settings/identity/service-accounts">
              Service accounts
            </Link>{" "}
            — machine identities and their credentials.
          </li>
        </ul>
      </section>
    </>
  );
}
