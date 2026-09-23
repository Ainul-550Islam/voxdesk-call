"use client";

import { useState } from "react";
import {
  DangerAction,
  ReauthPrompt,
  ResultBanner,
} from "@/components/identity/actions";
import { CheckField, InlineFacts, Section, SelectField, TextField } from "@/components/identity/fields";
import { Dash, EnforcementBadge } from "@/components/identity/badges";
import { useSubmit } from "@/components/identity/use-identity";
import { enforcementLabel, identityApi } from "@/lib/identity";
import type { DomainVerifyOut, EnterpriseDomain, SSOConnection } from "@/lib/identity-types";

/**
 * Enterprise domains.
 *
 * The panel keeps the two keys visibly separate, because merging them is the
 * mistake this feature exists to prevent: verifying a domain proves ownership
 * and changes nothing about how anyone signs in; enforcement is a second,
 * explicit decision and is refused while the domain is unverified.
 */
export default function DomainsPanel({
  domains,
  connections,
  onChanged,
}: {
  domains: EnterpriseDomain[];
  connections: SSOConnection[];
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [adding, setAdding] = useState(false);
  const [domain, setDomain] = useState("");
  const [challenge, setChallenge] = useState<EnterpriseDomain | null>(null);
  const [outcome, setOutcome] = useState<DomainVerifyOut | null>(null);

  async function add() {
    const ok = await submit.run(async () => {
      const created = await identityApi.addDomain(domain.trim());
      setChallenge(created);
      setAdding(false);
      setDomain("");
      onChanged();
    }, "Domain claimed. Publish the record, then verify.");
    if (!ok) setAdding(true);
  }

  async function reissue(row: EnterpriseDomain) {
    await submit.run(async () => {
      const issued = await identityApi.issueDomainChallenge(row.id);
      setChallenge(issued);
      setOutcome(null);
      onChanged();
    }, "New challenge issued. Any earlier record no longer counts.");
  }

  async function verify(row: EnterpriseDomain) {
    const ok = await submit.run(async () => {
      const result = await identityApi.verifyDomain(row.id);
      setOutcome(result);
      setChallenge(null);
      onChanged();
    }, "Verification checked.");
    if (!ok) {
      // A failed check is an ordinary answer, not an error: the panel shows the
      // resolver's own wording so the operator knows whether to wait or to fix
      // the record.
      setOutcome(null);
    }
  }

  async function setEnforcement(row: EnterpriseDomain, enforcement: string) {
    await submit.run(async () => {
      await identityApi.updateDomain(row.id, { enforcement });
      onChanged();
    }, `Enforcement set to ${enforcementLabel(enforcement)}.`);
  }

  async function togglePasswordLogin(row: EnterpriseDomain, block: boolean) {
    await submit.run(async () => {
      await identityApi.updateDomain(row.id, { block_password_login: block });
      onChanged();
    }, block ? "Password sign-in blocked for this domain." : "Password sign-in allowed again.");
  }

  async function bindConnection(row: EnterpriseDomain, connectionId: string) {
    await submit.run(async () => {
      await identityApi.updateDomain(row.id, {
        sso_connection_id: connectionId || null,
      });
      onChanged();
    }, "Connection bound to the domain.");
  }

  async function remove(row: EnterpriseDomain) {
    await submit.run(async () => {
      await identityApi.removeDomain(row.id);
      onChanged();
    }, "Domain removed. Password sign-in is restored if it was blocked.");
  }

  return (
    <>
      <Section
        title="Enterprise domains"
        description="Prove you own a domain with a DNS record, then decide — separately — whether it changes how people sign in."
        actions={
          <div className="section-actions">
            <button
              type="button"
              className="primary"
              disabled={submit.busy}
              onClick={() => {
                submit.clear();
                setAdding((value) => !value);
              }}
            >
              {adding ? "Cancel" : "Add domain"}
            </button>
          </div>
        }
      >
        <ResultBanner error={submit.error} notice={submit.notice} />

        {submit.needsReauth ? (
          <ReauthPrompt
            busy={submit.busy}
            canUseCode
            canUsePassword
            onReauthenticate={submit.reauthenticate}
          />
        ) : null}

        {adding ? (
          <div className="inline-form">
            <TextField
              id="new-domain"
              label="Domain"
              value={domain}
              onChange={setDomain}
              placeholder="acme.example.com"
              required
              hint="Without a scheme or path. A reserved name cannot be claimed."
            />
            <button
              type="button"
              className="primary"
              disabled={submit.busy || domain.trim().length < 4}
              onClick={() => void add()}
            >
              {submit.busy ? "Claiming…" : "Claim"}
            </button>
          </div>
        ) : null}

        {challenge?.verification_record_value ? (
          <div className="dns-record">
            <strong>Publish this DNS record</strong>
            <table className="record">
              <tbody>
                <tr>
                  <th>Type</th>
                  <td>TXT</td>
                </tr>
                <tr>
                  <th>Name</th>
                  <td>
                    <code>{challenge.verification_record_name}</code>
                  </td>
                </tr>
                <tr>
                  <th>Value</th>
                  <td>
                    <code>{challenge.verification_record_value}</code>
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="hint">
              Shown once: only its digest is stored, and re-issuing replaces it.
              DNS changes can take a few minutes to become visible.
            </p>
          </div>
        ) : null}

        {outcome ? (
          <p
            className={outcome.verified ? "notice" : "warn-banner"}
            role={outcome.verified ? "status" : "alert"}
          >
            {outcome.detail}
          </p>
        ) : null}

        {domains.length === 0 ? (
          <div className="empty">No domains claimed.</div>
        ) : (
          domains.map((row) => (
            <div className="domain-row" key={row.id}>
              <div className="domain-head">
                <strong>{row.domain}</strong>
                {row.verified ? (
                  <span className="badge completed">verified</span>
                ) : (
                  <span className="badge no_answer">
                    {row.verification_status || "unverified"}
                  </span>
                )}
                <EnforcementBadge enforcement={row.enforcement} />
              </div>
              <InlineFacts
                facts={[
                  { label: "Verified", value: row.verified_at ?? "not yet" },
                  { label: "Evidence", value: row.verification_record_name || <Dash /> },
                  { label: "Failed checks", value: row.failed_attempts },
                ]}
              />
              <div className="domain-controls">
                {!row.verified ? (
                  <>
                    <button
                      type="button"
                      className="primary"
                      disabled={submit.busy}
                      onClick={() => void verify(row)}
                    >
                      Check DNS now
                    </button>
                    <button
                      type="button"
                      disabled={submit.busy}
                      onClick={() => void reissue(row)}
                    >
                      New challenge
                    </button>
                  </>
                ) : (
                  <>
                    <SelectField
                      id={`enforcement-${row.id}`}
                      label="Enforcement"
                      value={row.enforcement}
                      onChange={(value) => void setEnforcement(row, value)}
                      options={[
                        { value: "off", label: "Off — proof only" },
                        { value: "warn", label: "Warn — allow passwords, record them" },
                        { value: "require_sso", label: "Require single sign-on" },
                      ]}
                      hint="Refused while the domain is unverified."
                    />
                    <CheckField
                      id={`block-${row.id}`}
                      label="Block password sign-in for this domain"
                      checked={row.block_password_login}
                      onChange={(value) => void togglePasswordLogin(row, value)}
                    />
                    {connections.length > 0 ? (
                      <SelectField
                        id={`conn-${row.id}`}
                        label="Single sign-on connection"
                        value={row.sso_connection_id ?? ""}
                        onChange={(value) => void bindConnection(row, value)}
                        options={[
                          { value: "", label: "Not bound" },
                          ...connections.map((connection) => ({
                            value: connection.id,
                            label: `${connection.name} (${connection.status})`,
                          })),
                        ]}
                      />
                    ) : null}
                  </>
                )}
                <DangerAction
                  label="Remove"
                  confirmLabel="Remove domain"
                  question={`Remove ${row.domain}? If it was enforcing single sign-on, password sign-in is restored.`}
                  onConfirm={() => void remove(row)}
                  busy={submit.busy}
                />
              </div>
            </div>
          ))
        )}
      </Section>
    </>
  );
}
