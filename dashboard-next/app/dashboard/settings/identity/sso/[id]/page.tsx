"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import IdentityNav from "@/components/identity/identity-nav";
import SsoConnectionDetail from "@/components/identity/sso-connection-detail";
import { Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { SSOConnection } from "@/lib/identity-types";

export default function SsoConnectionPage() {
  const params = useParams<{ id: string }>();
  const connectionId = typeof params?.id === "string" ? params.id : "";
  const connection = useLoader<SSOConnection>(
    () => identityApi.ssoConnection(connectionId),
    { enabled: connectionId.length > 0 },
  );

  return (
    <>
      <header className="page-head">
        <h1>Connection</h1>
        <p className="muted">
          <Link href="/dashboard/settings/identity/sso">← All connections</Link>
        </p>
      </header>

      <IdentityNav />

      {connection.error ? (
        <div className="error-banner">
          {connection.error}
          {" "}
          <Link href="/dashboard/settings/identity/sso">Back to connections</Link>
        </div>
      ) : connection.data ? (
        <SsoConnectionDetail
          connection={connection.data}
          onChanged={connection.reload}
        />
      ) : (
        <Section title="Connection">
          <p className="muted">Loading…</p>
        </Section>
      )}
    </>
  );
}
