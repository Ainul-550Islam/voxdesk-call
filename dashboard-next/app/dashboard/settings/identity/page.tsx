"use client";

import IdentityNav from "@/components/identity/identity-nav";
import PolicyPanel from "@/components/identity/policy-panel";
import { Dash } from "@/components/identity/badges";
import { InlineFacts, Section } from "@/components/identity/fields";
import { useLoader } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import { formatDateTime } from "@/lib/format";
import type { IdentityEvent, IdentityStatus } from "@/lib/identity-types";

/**
 * Identity settings overview.
 *
 * One call (`/api/identity/status`) drives the whole page: it already reports
 * the deployment's features, *this tenant's* policy, the caller's own factors
 * and session, and whether a fresh proof of presence is configured. Loading the
 * page from it means the controls can never offer something this deployment has
 * not compiled in.
 */
export default function IdentitySettingsPage() {
  const status = useLoader<IdentityStatus>(() => identityApi.status());
  const events = useLoader<IdentityEvent[]>(() => identityApi.events(25));

  if (status.error) return <div className="error-banner">{status.error}</div>;
  if (!status.data) return <div className="loading">Loading…</div>;

  const { features, policy } = status.data;

  return (
    <>
      <header className="page-head">
        <h1>Identity and access</h1>
        <p className="muted">
          How people sign in to this workspace, what a second factor is required
          for, and which machine credentials exist.
        </p>
      </header>

      <IdentityNav />

      <Section
        title="Deployment"
        description="What this installation supports. A feature that is off here is not offered anywhere in these pages."
      >
        <InlineFacts
          facts={[
            { label: "Two-factor", value: features.mfa_enabled ? "available" : "off" },
            { label: "Single sign-on", value: features.sso_enabled ? "available" : "off" },
            { label: "SCIM provisioning", value: features.scim_enabled ? "available" : "off" },
            {
              label: "Secret encryption",
              value: features.secrets_configured
                ? "configured"
                : "not configured — identity secrets cannot be stored",
            },
            {
              label: "Password sign-in",
              value: features.password_login_allowed ? "allowed" : "blocked by policy",
            },
            {
              label: "SSO required",
              value: features.sso_required ? "yes" : "no",
            },
          ]}
        />
      </Section>

      <PolicyPanel
        policy={policy}
        features={features}
        onChanged={status.reload}
      />

      <Section
        title="Your account"
        description="Your own factors and session. Self-service actions live under My security."
      >
        <InlineFacts
          facts={[
            {
              label: "Second factor",
              value: status.data.mfa.enrolled ? "enrolled" : "not enrolled",
            },
            {
              label: "Recovery codes",
              value: `${status.data.mfa.recovery_codes_remaining} unused`,
            },
            { label: "Email verified", value: status.data.email_verified ? "yes" : "no" },
            {
              label: "Fresh proof",
              value: status.data.reauth.required
                ? `required every ${status.data.reauth.fresh_minutes} minutes`
                : "not required",
            },
            { label: "This session", value: status.data.session.device || <Dash /> },
            { label: "Session idle timeout", value: status.data.session.idle_expires_at ?? <Dash /> },
          ]}
        />
      </Section>

      <Section
        title="Recent identity events"
        description="Authentication, factors, sessions, credentials and domains — from the same audit log everything else writes to."
      >
        {events.error ? <p className="error-banner">{events.error}</p> : null}
        {events.loading ? <p className="muted">Loading…</p> : null}
        {(events.data ?? []).length === 0 && !events.loading ? (
          <div className="empty">No identity events yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Event</th>
                  <th>Actor</th>
                  <th>Address</th>
                </tr>
              </thead>
              <tbody>
                {(events.data ?? []).map((event) => (
                  <tr key={event.id}>
                    <td>{formatDateTime(event.created_at)}</td>
                    <td>
                      {event.action.replace(/_/g, " ").toLowerCase()}
                    </td>
                    <td>{event.actor_email || <Dash />}</td>
                    <td>{event.ip_address || <Dash />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
