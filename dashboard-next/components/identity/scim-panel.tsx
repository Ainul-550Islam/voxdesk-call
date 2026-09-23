"use client";

import { useState } from "react";
import {
  DangerAction,
  ReauthPrompt,
  ResultBanner,
  SecretOnce,
} from "@/components/identity/actions";
import { CheckField, Section, TextField } from "@/components/identity/fields";
import { Dash, ScopeList } from "@/components/identity/badges";
import { useSubmit } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { ScimCredential, ScimCredentialCreated, SSOConnection } from "@/lib/identity-types";

//: The scopes the server accepts on a SCIM credential. Deliberately not RBAC
//: permission values: a provisioning token can reach users and groups in one
//: tenant and nothing else, and it can never call the product API.
const SCIM_SCOPES = ["scim:users", "scim:groups"];

/**
 * SCIM provisioning.
 *
 * The token a provider authenticates with is a *separate credential family*
 * from API keys and user sessions, and the panel says so, because the mistake
 * this prevents is real: a super-admin JWT pasted into an IdP's settings and
 * never rotated.
 */
export default function ScimPanel({
  credentials,
  connections,
  onChanged,
}: {
  credentials: ScimCredential[];
  connections: SSOConnection[];
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [creating, setCreating] = useState(false);
  const [label, setLabel] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [scopes, setScopes] = useState<string[]>([...SCIM_SCOPES]);
  const [expiresInDays, setExpiresInDays] = useState("365");
  const [token, setToken] = useState<ScimCredentialCreated | null>(null);

  async function create() {
    const ok = await submit.run(async () => {
      const issued = await identityApi.createScimCredential({
        label: label.trim(),
        connection_id: connectionId || null,
        scopes,
        expires_in_days: expiresInDays ? Number(expiresInDays) : null,
      });
      setToken(issued);
      setCreating(false);
      setLabel("");
      setConnectionId("");
      onChanged();
    }, "SCIM token issued. Copy it now — it is stored as a hash.");
    if (!ok) setCreating(true);
  }

  async function rotate(credentialId: string) {
    await submit.run(async () => {
      const issued = await identityApi.rotateScimCredential(credentialId);
      setToken(issued);
      onChanged();
    }, "Token rotated. The previous token stopped working immediately.");
  }

  async function revoke(credentialId: string) {
    await submit.run(async () => {
      await identityApi.revokeScimCredential(credentialId);
      onChanged();
    }, "SCIM token revoked. The provider will start receiving 401s.");
  }

  return (
    <Section
      title="SCIM provisioning"
      description="Directory synchronisation: the provider creates, updates and deactivates people here. Deactivation revokes sessions immediately."
      actions={
        <div className="section-actions">
          <button
            type="button"
            className="primary"
            disabled={submit.busy}
            onClick={() => {
              submit.clear();
              setCreating((value) => !value);
            }}
          >
            {creating ? "Cancel" : "New token"}
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

      {token ? (
        <SecretOnce
          label={`SCIM token ${token.prefix}…`}
          secret={token.token}
          warning={token.warning}
          onDismiss={() => setToken(null)}
          extra={
            <p className="hint">
              Base URL for the provider:{" "}
              <code>{identityApi.scimBaseUrl(token.connection_id)}</code>
            </p>
          }
        />
      ) : null}

      {creating ? (
        <div className="create-form">
          <TextField
            id="scim-label"
            label="Label"
            value={label}
            onChange={setLabel}
            placeholder="Okta provisioning"
            hint="Which provider holds this token."
          />
          <div className="field">
            <label htmlFor="scim-connection">Bound connection</label>
            <select
              id="scim-connection"
              value={connectionId}
              onChange={(event) => setConnectionId(event.target.value)}
            >
              <option value="">Not bound (uses “default” in the path)</option>
              {connections.map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.name} ({connection.status})
                </option>
              ))}
            </select>
            <p className="hint">
              Binding a token to a connection keeps the path it posts to stable.
            </p>
          </div>
          <fieldset className="scope-picker">
            <legend>Scopes</legend>
            {SCIM_SCOPES.map((scope) => (
              <CheckField
                key={scope}
                id={`scim-${scope}`}
                label={scope}
                checked={scopes.includes(scope)}
                onChange={(on) =>
                  setScopes((current) =>
                    on ? [...new Set([...current, scope])] : current.filter((s) => s !== scope),
                  )
                }
              />
            ))}
            <p className="hint">
              A provisioning token can never call the product API, and never
              carries a role.
            </p>
          </fieldset>
          <TextField
            id="scim-expiry"
            label="Expires in (days)"
            type="number"
            value={expiresInDays}
            onChange={setExpiresInDays}
            hint="Rotating a provisioning token on a schedule is the point; give it an expiry so a forgotten one fails closed."
          />
          <button
            type="button"
            className="primary"
            disabled={submit.busy || scopes.length === 0}
            onClick={() => void create()}
          >
            {submit.busy ? "Issuing…" : "Issue token"}
          </button>
        </div>
      ) : null}

      {credentials.length === 0 ? (
        <div className="empty">No provisioning tokens.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Label</th>
                <th>Prefix</th>
                <th>Scopes</th>
                <th>Base URL</th>
                <th>Expires</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {credentials.map((row) => (
                <tr key={row.id} className={row.revoked_at ? "revoked" : undefined}>
                  <td>
                    {row.label || <Dash />}
                    {row.revoked_at ? <span className="badge failed">revoked</span> : null}
                  </td>
                  <td>
                    <code>{row.prefix}…</code>
                  </td>
                  <td>
                    <ScopeList scopes={row.scopes} />
                  </td>
                  <td>
                    <code>{identityApi.scimBaseUrl(row.connection_id)}</code>
                  </td>
                  <td>{row.expires_at ?? "never"}</td>
                  <td>{row.last_used_at ?? "never"}</td>
                  <td className="row-actions">
                    {row.revoked_at ? (
                      <span className="muted small">revoked</span>
                    ) : (
                      <>
                        <button
                          type="button"
                          disabled={submit.busy}
                          onClick={() => void rotate(row.id)}
                        >
                          Rotate
                        </button>
                        <DangerAction
                          label="Revoke"
                          confirmLabel="Revoke token"
                          question="The provider keeps its schedule, so it will not notice until its next run: users will stop syncing and deactivations will stop arriving."
                          onConfirm={() => void revoke(row.id)}
                          busy={submit.busy}
                        />
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="hint">
        Resources live at <code>/scim/v2/&lt;connection&gt;/Users</code>,{" "}
        <code>/Groups</code>, <code>/ServiceProviderConfig</code>,{" "}
        <code>/ResourceTypes</code> and <code>/Schemas</code>. A person in
        another workspace answers 404, exactly like one that does not exist.
      </p>
    </Section>
  );
}
