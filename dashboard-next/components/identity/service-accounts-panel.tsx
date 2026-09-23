"use client";

import { useState } from "react";
import {
  DangerAction,
  ReauthPrompt,
  ResultBanner,
  SecretOnce,
} from "@/components/identity/actions";
import { CheckField, InlineFacts, Section, TextField } from "@/components/identity/fields";
import { Dash, ScopeList, StatusBadge } from "@/components/identity/badges";
import { useLoader, useSubmit } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type {
  CredentialIn,
  MachineCredential,
  ScopeCatalogue,
  ServiceAccount,
} from "@/lib/identity-types";

/**
 * Service accounts: machine identities with their own scopes and credentials.
 *
 * The three states are shown as three states, because conflating them is how an
 * incident goes wrong: `disabled` is the workspace's own switch and any
 * administrator can undo it; `emergency_disabled` is the platform's stop and
 * only an **owner**, with a written reason, can lift it; and a deletion is
 * final. The panel also refuses to pretend the enable button can lift a stop —
 * the server rejects that, and the UI says so before the click.
 */
export default function ServiceAccountsPanel({
  accounts,
  catalogue,
  onChanged,
}: {
  accounts: ServiceAccount[];
  catalogue: ScopeCatalogue | null;
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [selected, setSelected] = useState<string | null>(null);
  const [secret, setSecret] = useState<{
    label: string;
    value: string;
    warning: string;
  } | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresInDays, setExpiresInDays] = useState("");

  const grantable = catalogue?.grantable ?? [];
  const account = accounts.find((row) => row.id === selected) ?? null;

  async function create() {
    const ok = await submit.run(async () => {
      const created = await identityApi.createServiceAccount({
        name: name.trim(),
        description: description.trim(),
        scopes,
        expires_in_days: expiresInDays ? Number(expiresInDays) : null,
      });
      setSelected(created.id);
      setCreating(false);
      setName("");
      setDescription("");
      setScopes([]);
      setExpiresInDays("");
      onChanged();
    }, "Service account created. Give it a credential next.");
    if (!ok) setCreating(true);
  }

  return (
    <>
      <Section
        title="Service accounts"
        description="A machine identity with its own scopes, owner and expiry. It can hold several credentials, and it can never act as a person."
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
              {creating ? "Cancel" : "New service account"}
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
            label={secret.label}
            secret={secret.value}
            warning={secret.warning}
            onDismiss={() => setSecret(null)}
          />
        ) : null}

        {creating ? (
          <div className="create-form">
            <TextField
              id="sa-name"
              label="Name"
              value={name}
              onChange={setName}
              placeholder="Nightly sync"
              required
              hint="Unique in this workspace. Name it after the thing that uses it."
            />
            <TextField
              id="sa-description"
              label="Description"
              value={description}
              onChange={setDescription}
              placeholder="Imports call outcomes into the warehouse"
            />
            <TextField
              id="sa-expiry"
              label="Expires in (days)"
              type="number"
              value={expiresInDays}
              onChange={setExpiresInDays}
              placeholder="never"
              hint="Optional. Worth setting for a migration or a pilot."
            />
            <fieldset className="scope-picker">
              <legend>Scopes</legend>
              {grantable.map((scope) => (
                <CheckField
                  key={scope}
                  id={`sa-scope-${scope}`}
                  label={scope}
                  checked={scopes.includes(scope)}
                  onChange={(on) =>
                    setScopes((current) =>
                      on
                        ? [...new Set([...current, scope])]
                        : current.filter((item) => item !== scope),
                    )
                  }
                />
              ))}
              <p className="hint">
                The scopes are the ceiling on everything the account can do, and
                they are re-checked against the owner on every request.
              </p>
            </fieldset>
            <button
              type="button"
              className="primary"
              disabled={submit.busy || name.trim().length === 0 || scopes.length === 0}
              onClick={() => void create()}
            >
              {submit.busy ? "Creating…" : "Create"}
            </button>
          </div>
        ) : null}

        {accounts.length === 0 ? (
          <div className="empty">No service accounts.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Scopes</th>
                  <th>State</th>
                  <th>Credentials</th>
                  <th>Last used</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {accounts.map((row) => (
                  <tr
                    key={row.id}
                    className={row.id === selected ? "selected" : undefined}
                  >
                    <td>
                      {row.name}
                      {row.description ? (
                        <div className="muted small">{row.description}</div>
                      ) : null}
                      <div className="muted small">{row.scope_summary}</div>
                    </td>
                    <td>
                      <ScopeList scopes={row.scopes} />
                    </td>
                    <td>
                      {row.emergency_disabled ? (
                        <StatusBadge status="disabled" label="stopped by operator" />
                      ) : row.enabled ? (
                        <StatusBadge status="active" label="enabled" />
                      ) : (
                        <StatusBadge status="disabled" label="disabled" />
                      )}
                      {row.disabled_reason ? (
                        <div className="muted small">{row.disabled_reason}</div>
                      ) : null}
                    </td>
                    <td>{row.credential_count}</td>
                    <td>{row.last_used_at ?? "never"}</td>
                    <td className="row-actions">
                      <button
                        type="button"
                        onClick={() => setSelected(row.id === selected ? null : row.id)}
                      >
                        {row.id === selected ? "Close" : "Manage"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      {account ? (
        <AccountDetail
          account={account}
          onChanged={onChanged}
          onSecret={(label, value, warning) => setSecret({ label, value, warning })}
        />
      ) : null}
    </>
  );
}

function AccountDetail({
  account,
  onChanged,
  onSecret,
}: {
  account: ServiceAccount;
  onChanged: () => void;
  onSecret: (label: string, value: string, warning: string) => void;
}) {
  const submit = useSubmit();
  const credentials = useLoader<MachineCredential[]>(
    () => identityApi.serviceAccountCredentials(account.id),
    { enabled: Boolean(account.id) },
  );
  const [label, setLabel] = useState("");
  const [expiresInDays, setExpiresInDays] = useState("");

  async function createCredential(body: CredentialIn) {
    await submit.run(async () => {
      const issued = await identityApi.createServiceAccountCredential(
        account.id,
        body,
      );
      onSecret(
        `Credential for ${account.name}`,
        issued.secret,
        issued.warning,
      );
      setLabel("");
      setExpiresInDays("");
      credentials.reload();
      onChanged();
    }, "Credential created.");
  }

  async function rotateCredential(credential: MachineCredential) {
    await submit.run(async () => {
      const issued = await identityApi.rotateServiceAccountCredential(
        account.id,
        credential.id,
      );
      onSecret(
        `Replacement credential for ${account.name}`,
        issued.secret,
        issued.warning,
      );
      credentials.reload();
      onChanged();
    }, "Credential rotated. The previous secret stopped working.");
  }

  async function revokeCredential(credential: MachineCredential) {
    await submit.run(async () => {
      await identityApi.revokeServiceAccountCredential(account.id, credential.id);
      credentials.reload();
      onChanged();
    }, "Credential revoked.");
  }

  async function setEnabled(enabled: boolean) {
    await submit.run(async () => {
      if (enabled) {
        await identityApi.enableServiceAccount(account.id);
      } else {
        await identityApi.disableServiceAccount(account.id, {
          reason: "disabled_by_admin",
        });
      }
      onChanged();
    }, enabled ? "Service account enabled." : "Service account disabled.");
  }

  return (
    <Section
      title={`Service account: ${account.name}`}
      description="Credentials authenticate as this identity; its owner's permissions are re-checked on every request."
      actions={
        <div className="section-actions">
          {account.enabled ? (
            <DangerAction
              label="Disable"
              confirmLabel="Disable account"
              question="Every credential stops working until it is enabled again."
              onConfirm={() => void setEnabled(false)}
              busy={submit.busy}
            />
          ) : (
            <button
              type="button"
              className="primary"
              disabled={submit.busy || account.emergency_disabled}
              onClick={() => void setEnabled(true)}
            >
              Enable
            </button>
          )}
          <DangerAction
            label="Emergency stop"
            confirmLabel="Trip the stop"
            question="This is the platform-level stop: the account is disabled and only an owner can clear it, with a written reason."
            reasonLabel="Reason"
            reasonPlaceholder="Suspected credential leak"
            reasonMinLength={8}
            onConfirm={(reason) =>
              void submit.run(async () => {
                await identityApi.emergencyDisableServiceAccount(account.id, reason);
                onChanged();
              }, "Emergency stop tripped.")
            }
            busy={submit.busy}
          />
          <DangerAction
            label="Delete"
            confirmLabel="Delete account"
            question="The identity is removed and every credential and key with it. The audit trail is kept."
            onConfirm={() =>
              void submit.run(async () => {
                await identityApi.deleteServiceAccount(account.id);
                onChanged();
              }, "Service account deleted.")
            }
            busy={submit.busy}
          />
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

      {account.emergency_disabled ? (
        <div className="stop-banner" role="alert">
          <strong>Stopped by the platform operator.</strong>
          <p className="hint">
            The workspace switch cannot lift this. Only an owner can clear it,
            and the reason is recorded in the audit log.
          </p>
          <DangerAction
            label="Clear the stop"
            confirmLabel="Clear the stop"
            question="Put this account back into service? Its credentials start working again immediately."
            reasonLabel="Reason for clearing"
            reasonPlaceholder="False positive; investigating the alert"
            reasonMinLength={8}
            onConfirm={(reason) =>
              void submit.run(async () => {
                await identityApi.emergencyClearServiceAccount(account.id, reason);
                onChanged();
              }, "Emergency stop cleared.")
            }
            busy={submit.busy}
          />
        </div>
      ) : null}

      <InlineFacts
        facts={[
          { label: "Owner", value: account.owner_user_id ?? <Dash /> },
          { label: "Created", value: account.created_at ?? <Dash /> },
          { label: "Expires", value: account.expires_at ?? "never" },
          { label: "Last used", value: account.last_used_at ?? "never" },
        ]}
      />

      <h3>Credentials</h3>
      <div className="inline-form">
        <TextField
          id="cred-label"
          label="Label"
          value={label}
          onChange={setLabel}
          placeholder="Warehouse loader"
          hint="What will hold this secret. Revoking one should not break another."
        />
        <TextField
          id="cred-expiry"
          label="Expires in (days)"
          type="number"
          value={expiresInDays}
          onChange={setExpiresInDays}
          placeholder="never"
        />
        <button
          type="button"
          className="primary"
          disabled={submit.busy}
          onClick={() =>
            void createCredential({
              label: label.trim(),
              expires_in_days: expiresInDays ? Number(expiresInDays) : null,
            })
          }
        >
          Add credential
        </button>
      </div>

      {credentials.error ? (
        <p className="error-banner">{credentials.error}</p>
      ) : credentials.loading ? (
        <p className="muted">Loading credentials…</p>
      ) : (credentials.data ?? []).length === 0 ? (
        <div className="empty">No credentials yet.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Label</th>
                <th>Prefix</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(credentials.data ?? []).map((row) => (
                <tr key={row.id}>
                  <td>{row.label || <Dash />}</td>
                  <td>
                    <code>{row.prefix}…</code>
                  </td>
                  <td>{row.created_at ?? <Dash />}</td>
                  <td>{row.expires_at ?? "never"}</td>
                  <td>{row.last_used_at ?? "never"}</td>
                  <td className="row-actions">
                    <button
                      type="button"
                      disabled={submit.busy}
                      onClick={() => void rotateCredential(row)}
                    >
                      Rotate
                    </button>
                    <DangerAction
                      label="Revoke"
                      confirmLabel="Revoke credential"
                      question="Anything using this credential stops working immediately."
                      onConfirm={() => void revokeCredential(row)}
                      busy={submit.busy}
                    />
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

