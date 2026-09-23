"use client";

import Link from "next/link";
import { useState } from "react";
import { DangerAction, ResultBanner } from "@/components/identity/actions";
import { Section, TextField } from "@/components/identity/fields";
import { Dash, ProtocolBadge, StatusBadge } from "@/components/identity/badges";
import { useSubmit } from "@/components/identity/use-identity";
import { identityApi, slugify } from "@/lib/identity";
import type { SSOAttempt, SSOConnection, SSOConnectionIn } from "@/lib/identity-types";

/** The connections an operator can actually pick: anything not disabled. */
export function liveConnections(connections: SSOConnection[]): SSOConnection[] {
  return connections.filter((row) => row.status !== "disabled");
}

/**
 * Single sign-on: the connections list, a create form, and the attempt feed.
 *
 * Configuration and per-connection detail (certificates, claim mappings, the
 * SP metadata) live on the connection's own page, because a list that also
 * edits is how an operator changes the wrong row.
 */
export default function SsoPanel({
  connections,
  attempts,
  onChanged,
}: {
  connections: SSOConnection[];
  attempts: SSOAttempt[];
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [protocol, setProtocol] = useState("oidc");

  async function create() {
    const body: SSOConnectionIn = {
      name: name.trim(),
      slug: slugify(name),
      protocol,
    };
    const ok = await submit.run(async () => {
      await identityApi.createSsoConnection(body);
      setCreating(false);
      setName("");
      onChanged();
    }, "Connection created as a draft. Configure it, then activate it.");
    if (!ok) setCreating(true);
  }

  async function setStatus(row: SSOConnection, status: "active" | "disabled" | "draft") {
    await submit.run(async () => {
      await identityApi.setSsoConnectionStatus(row.id, status);
      onChanged();
    }, `Connection is now ${status}.`);
  }

  async function remove(row: SSOConnection) {
    await submit.run(async () => {
      await identityApi.deleteSsoConnection(row.id);
      onChanged();
    }, "Connection removed.");
  }

  return (
    <>
      <Section
        title="Single sign-on connections"
        description="A connection starts as a draft and affects nobody until it is activated. Disabling one takes effect immediately."
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
              {creating ? "Cancel" : "New connection"}
            </button>
          </div>
        }
      >
        <ResultBanner error={submit.error} notice={submit.notice} />

        {creating ? (
          <div className="create-form">
            <TextField
              id="conn-name"
              label="Display name"
              value={name}
              onChange={setName}
              placeholder="Okta"
              required
              hint={`The sign-in URL uses the slug: /auth/sso/${slugify(name) || "…"}/start`}
            />
            <div className="field">
              <label htmlFor="conn-protocol">Protocol</label>
              <select
                id="conn-protocol"
                value={protocol}
                onChange={(event) => setProtocol(event.target.value)}
              >
                <option value="oidc">OpenID Connect</option>
                <option value="saml">SAML 2.0</option>
              </select>
            </div>
            <button
              type="button"
              className="primary"
              disabled={submit.busy || name.trim().length < 1}
              onClick={() => void create()}
            >
              {submit.busy ? "Creating…" : "Create draft"}
            </button>
          </div>
        ) : null}

        {connections.length === 0 ? (
          <div className="empty">No connections yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Protocol</th>
                  <th>Status</th>
                  <th>Certificates</th>
                  <th>Created</th>
                  <th>Last login</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {connections.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Link href={`/dashboard/settings/identity/sso/${row.id}`}>
                        {row.name}
                      </Link>
                      <div className="muted small">
                        <code>{row.slug}</code>
                      </div>
                    </td>
                    <td>
                      <ProtocolBadge protocol={row.protocol} />
                    </td>
                    <td>
                      <StatusBadge status={row.status} />
                    </td>
                    <td>{row.protocol === "saml" ? row.active_certificates : <Dash />}</td>
                    <td>{row.created_at ?? <Dash />}</td>
                    <td>{row.last_login_at ?? "never"}</td>
                    <td className="row-actions">
                      {row.status === "active" ? (
                        <button
                          type="button"
                          disabled={submit.busy}
                          onClick={() => void setStatus(row, "disabled")}
                        >
                          Disable
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="primary"
                          disabled={submit.busy}
                          onClick={() => void setStatus(row, "active")}
                        >
                          Activate
                        </button>
                      )}
                      <DangerAction
                        label="Delete"
                        confirmLabel="Delete connection"
                        question={`Delete ${row.name}? Anyone signing in through it will be refused immediately.`}
                        onConfirm={() => void remove(row)}
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

      <Section
        title="Recent federated sign-ins"
        description="Every attempt: the connection, the protocol, the outcome, and — when it failed — a short reason. No token, assertion or secret is recorded."
      >
        {attempts.length === 0 ? (
          <div className="empty">No attempts recorded.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Protocol</th>
                  <th>Kind</th>
                  <th>Outcome</th>
                  <th>Reason</th>
                  <th>Address</th>
                </tr>
              </thead>
              <tbody>
                {attempts.map((row) => (
                  <tr key={row.id}>
                    <td>{row.created_at ?? <Dash />}</td>
                    <td>
                      <ProtocolBadge protocol={row.protocol} />
                    </td>
                    <td>{row.kind}</td>
                    <td>
                      <StatusBadge status={row.outcome} />
                    </td>
                    <td>{row.failure_reason || <Dash />}</td>
                    <td>{row.ip_address || <Dash />}</td>
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

/** Shared by the list and the detail page: a quick self-test of a connection. */
export function SsoTestButton({ connectionId }: { connectionId: string }) {
  const submit = useSubmit();
  return (
    <div>
      <button
        type="button"
        disabled={submit.busy}
        onClick={() =>
          void submit.run(async () => {
            const result = await identityApi.testSsoConnection(connectionId);
            if (!result.ok) {
              throw new Error(result.detail || "The check did not pass.");
            }
          }, "The connection answered as configured.")
        }
      >
        {submit.busy ? "Checking…" : "Run configuration check"}
      </button>
      <ResultBanner error={submit.error} notice={submit.notice} />
    </div>
  );
}
