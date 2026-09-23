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
import type { ApiKey, ApiKeyCreated, ScopeCatalogue } from "@/lib/identity-types";

/**
 * API keys: the tokens a script or an integration authenticates with.
 *
 * Two things this panel is deliberate about:
 *
 * * The secret is shown exactly once, by the call that created it. There is no
 *   "reveal" action, because the server stores only a hash — offering one would
 *   be a lie.
 * * The scope list comes from `GET /api/api-keys/scopes`, so the picker can
 *   only offer what *this* caller is allowed to grant. The server validates the
 *   same list again; the picker exists to make the refusal impossible rather
 *   than to be the check.
 */
export default function ApiKeysPanel({
  keys,
  catalogue,
  onChanged,
}: {
  keys: ApiKey[];
  catalogue: ScopeCatalogue | null;
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresInDays, setExpiresInDays] = useState("");
  const [secret, setSecret] = useState<ApiKeyCreated | null>(null);
  const [includeRevoked, setIncludeRevoked] = useState(false);

  const grantable = catalogue?.grantable ?? [];

  function toggle(scope: string, on: boolean) {
    setScopes((current) =>
      on ? [...new Set([...current, scope])] : current.filter((s) => s !== scope),
    );
  }

  async function create() {
    const ok = await submit.run(async () => {
      const issued = await identityApi.createApiKey({
        name: name.trim(),
        scopes,
        expires_in_days: expiresInDays ? Number(expiresInDays) : null,
      });
      setSecret(issued);
      setCreating(false);
      setName("");
      setScopes([]);
      setExpiresInDays("");
      onChanged();
    }, "API key created. Copy the secret now — it is not stored.");
    if (!ok) setCreating(true);
  }

  async function rotate(keyId: string) {
    await submit.run(async () => {
      const issued = await identityApi.rotateApiKey(keyId);
      setSecret(issued);
      onChanged();
    }, "Key rotated. The old secret stopped working immediately.");
  }

  async function revoke(keyId: string) {
    await submit.run(async () => {
      await identityApi.revokeApiKey(keyId);
      onChanged();
    }, "API key revoked.");
  }

  const rows = includeRevoked ? keys : keys.filter((key) => !key.revoked_at);

  return (
    <Section
      title="API keys"
      description="Each key carries explicit scopes and is stored as a hash, so it can never be read back — only rotated or revoked."
      actions={
        <div className="section-actions">
          <CheckField
            id="include-revoked"
            label="Show revoked"
            checked={includeRevoked}
            onChange={setIncludeRevoked}
          />
          <button
            type="button"
            className="primary"
            disabled={submit.busy}
            onClick={() => {
              submit.clear();
              setCreating((value) => !value);
            }}
          >
            {creating ? "Cancel" : "Create key"}
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

      {secret ? (
        <SecretOnce
          label={`Key “${secret.name}” (${secret.prefix}…)`}
          secret={secret.secret}
          warning={secret.warning}
          onDismiss={() => setSecret(null)}
        />
      ) : null}

      {creating ? (
        <div className="create-form">
          <TextField
            id="key-name"
            label="Name"
            value={name}
            onChange={setName}
            placeholder="Reporting job"
            required
            hint="What will use it. A name is how you find the key later — the secret itself is indistinguishable between keys."
          />
          <TextField
            id="key-expiry"
            label="Expires in (days)"
            type="number"
            value={expiresInDays}
            onChange={setExpiresInDays}
            placeholder="never"
            hint="Optional, and worth setting for anything temporary: an expired key fails closed with nobody doing anything."
          />
          <fieldset className="scope-picker">
            <legend>Scopes</legend>
            {grantable.length === 0 ? (
              <p className="muted">
                No scope list is available. Reload the page; the server decides
                what this account may grant.
              </p>
            ) : (
              grantable.map((scope) => (
                <CheckField
                  key={scope}
                  id={`scope-${scope}`}
                  label={scope}
                  checked={scopes.includes(scope)}
                  onChange={(on) => toggle(scope, on)}
                />
              ))
            )}
            <p className="hint">
              A key can never hold more than the person who created it. Asking
              for a scope you do not hold is refused, not trimmed.
            </p>
          </fieldset>
          <button
            type="button"
            className="primary"
            disabled={submit.busy || name.trim().length === 0 || scopes.length === 0}
            onClick={() => void create()}
          >
            {submit.busy ? "Creating…" : "Create key"}
          </button>
        </div>
      ) : null}

      {rows.length === 0 ? (
        <div className="empty">No API keys.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Prefix</th>
                <th>Scopes</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((key) => (
                <tr key={key.id} className={key.revoked_at ? "revoked" : undefined}>
                  <td>
                    {key.name}
                    {key.revoked_at ? (
                      <span className="badge failed">revoked</span>
                    ) : null}
                    {key.service_account_id ? (
                      <div className="muted small">
                        belongs to a service account
                      </div>
                    ) : null}
                  </td>
                  <td>
                    <code>{key.prefix}…</code>
                  </td>
                  <td>
                    <ScopeList scopes={key.scopes} />
                  </td>
                  <td>{key.created_at ?? <Dash />}</td>
                  <td>{key.expires_at ?? "never"}</td>
                  <td>{key.last_used_at ?? "never"}</td>
                  <td className="row-actions">
                    {!key.revoked_at ? (
                      <>
                        <button
                          type="button"
                          disabled={submit.busy}
                          onClick={() => void rotate(key.id)}
                        >
                          Rotate
                        </button>
                        <DangerAction
                          label="Revoke"
                          confirmLabel="Revoke key"
                          question={`Revoke “${key.name}”? Anything using it stops working immediately.`}
                          onConfirm={() => void revoke(key.id)}
                          busy={submit.busy}
                        />
                      </>
                    ) : (
                      <span className="muted small">
                        {key.revoked_reason || "revoked"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
