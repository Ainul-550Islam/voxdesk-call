"use client";

import { useState } from "react";
import IdentityNav from "@/components/identity/identity-nav";
import MfaPanel from "@/components/identity/mfa-panel";
import SessionsPanel from "@/components/identity/sessions-panel";
import { InlineFacts, Section, TextField } from "@/components/identity/fields";
import { ResultBanner } from "@/components/identity/actions";
import { useLoader, useSubmit } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { IdentityStatus, SessionList } from "@/lib/identity-types";

/**
 * The caller's own security: factors, devices, and the password.
 *
 * This page is self-service on purpose — every route behind it is reachable by
 * any signed-in person for their *own* account, which is why it is separate
 * from the workspace-wide settings a viewer may not touch.
 */
export default function MySecurityPage() {
  const status = useLoader<IdentityStatus>(() => identityApi.status());
  const sessions = useLoader<SessionList>(() => identityApi.sessions());
  const reset = useSubmit();
  const [email, setEmail] = useState("");

  if (status.error) return <div className="error-banner">{status.error}</div>;
  if (!status.data) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>My security</h1>
        <p className="muted">
          Your password, your second factor, and everywhere your account is
          signed in.
        </p>
      </header>

      <IdentityNav />

      <MfaPanel status={status.data.mfa} />

      {sessions.error ? (
        <div className="error-banner">{sessions.error}</div>
      ) : sessions.data ? (
        <SessionsPanel
          sessions={sessions.data}
          currentSessionId={status.data.session.session_id}
          onChanged={() => {
            sessions.reload();
            status.reload();
          }}
        />
      ) : (
        <Section title="Devices and sessions">
          <p className="muted">Loading…</p>
        </Section>
      )}

      <Section
        title="Password"
        description="There is no change-password form here on purpose: a reset link is the one path that proves the address belongs to you, and it ends every session established with the old password."
      >
        <InlineFacts
          facts={[
            {
              label: "Email",
              value: status.data.email_verified ? "verified" : "on file, not verified",
            },
          ]}
        />
        <ResultBanner error={reset.error} notice={reset.notice} />
        {!status.data.email_verified ? (
          <div className="action-row">
            <button
              type="button"
              disabled={reset.busy}
              onClick={() =>
                void reset.run(async () => {
                  await identityApi.requestEmailVerification();
                }, "Verification message sent. Check your inbox.")
              }
            >
              {reset.busy ? "Sending…" : "Send me a verification link"}
            </button>
          </div>
        ) : null}
        <div className="inline-form">
          <TextField
            id="reset-email"
            label="Send a password reset link to"
            type="email"
            value={email}
            onChange={setEmail}
            placeholder="you@company.com"
            hint="The answer is the same whether or not an account exists for that address."
          />
          <button
            type="button"
            disabled={reset.busy || !email.includes("@")}
            onClick={() =>
              void reset.run(async () => {
                await identityApi.requestPasswordReset(email.trim());
                setEmail("");
              }, "If that address has an account, a message is on its way.")
            }
          >
            {reset.busy ? "Sending…" : "Send reset link"}
          </button>
        </div>
      </Section>

      <Section
        title="Session policy in force"
        description="What this workspace imposes on sessions like yours."
      >
        <InlineFacts
          facts={[
            {
              label: "Idle timeout",
              value: `${status.data.policy.session_idle_minutes} minutes`,
            },
            {
              label: "Absolute lifetime",
              value: `${status.data.policy.refresh_token_days} days`,
            },
            {
              label: "Open sessions allowed",
              value: status.data.policy.session_max_active,
            },
            {
              label: "Fresh proof required",
              value: status.data.reauth.required
                ? `yes, every ${status.data.reauth.fresh_minutes} minutes`
                : "no",
            },
            { label: "This session verifies", value: status.data.session.mfa_verified ? "with a factor" : "with a password" },
            { label: "Session expires", value: status.data.session.expires_at ?? "—" },
          ]}
        />
      </Section>
    </>
  );
}
