"use client";

import { useState } from "react";
import { DangerAction, ReauthPrompt, ResultBanner } from "@/components/identity/actions";
import { CheckField, InlineFacts, Section, TextAreaField, TextField } from "@/components/identity/fields";
import { Dash, ProtocolBadge, StatusBadge } from "@/components/identity/badges";
import { useLoader, useSubmit } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type {
  SSOCertificate,
  SSOConnection,
  SSOConnectionIn,
  SSOTestOut,
} from "@/lib/identity-types";

/**
 * One connection, in full.
 *
 * Order top to bottom mirrors the order an operator works in: identity at the
 * provider, then the claim mapping, then the signing certificate (SAML), then
 * the metadata to hand back. Activation is last because every check above it
 * has to pass first.
 */
export default function SsoConnectionDetail({
  connection,
  onChanged,
}: {
  connection: SSOConnection;
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const certificates = useLoader<SSOCertificate[]>(
    () => identityApi.ssoCertificates(connection.id),
    { enabled: connection.protocol === "saml" },
  );
  const [test, setTest] = useState<SSOTestOut | null>(null);
  const [cert, setCert] = useState("");
  const [edits, setEdits] = useState<Partial<SSOConnectionIn>>({});
  const [roleMap, setRoleMap] = useState(
    Object.entries(connection.role_mapping)
      .map(([key, value]) => `${key}=${value}`)
      .join("\n"),
  );
  const [groupMap, setGroupMap] = useState(
    Object.entries(connection.group_mapping)
      .map(([key, value]) => `${key}=${value}`)
      .join("\n"),
  );

  /**
   * The current value of a field: the operator's unsaved edit if there is one,
   * otherwise what the server sent. Written this way so a field that was never
   * touched is sent back untouched rather than as an empty string.
   */
  const value = <K extends keyof SSOConnectionIn>(
    key: K,
    fallback: SSOConnectionIn[K],
  ): SSOConnectionIn[K] => {
    const edited = edits[key];
    return (edited === undefined ? fallback : edited) as SSOConnectionIn[K];
  };

  function parseMapping(text: string): Record<string, string> {
    const result: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const separator = trimmed.indexOf("=");
      if (separator < 1) continue;
      result[trimmed.slice(0, separator).trim()] = trimmed
        .slice(separator + 1)
        .trim()
        .toLowerCase();
    }
    return result;
  }

  async function save() {
    const change = { ...edits };
    const ok = await submit.run(async () => {
      if (Object.keys(change).length > 0) {
        await identityApi.updateSsoConnection(connection.id, change);
        setEdits({});
      }
      await identityApi.setSsoMappings(connection.id, {
        role_mapping: parseMapping(roleMap),
        group_mapping: parseMapping(groupMap),
        default_role: connection.default_role,
        deny_unmapped_roles: connection.deny_unmapped_roles,
      });
      onChanged();
    }, "Connection saved.");
    if (!ok) setEdits(change);
  }

  async function runTest() {
    await submit.run(async () => {
      const result = await identityApi.testSsoConnection(connection.id);
      setTest(result);
    });
  }

  async function addCertificate() {
    await submit.run(async () => {
      await identityApi.addSsoCertificate(connection.id, cert.trim());
      setCert("");
      certificates.reload();
      onChanged();
    }, "Certificate registered.");
  }

  async function retireCertificate(certificateId: string) {
    await submit.run(async () => {
      await identityApi.retireSsoCertificate(connection.id, certificateId);
      certificates.reload();
      onChanged();
    }, "Certificate retired.");
  }

  async function activate(status: "active" | "disabled" | "draft") {
    await submit.run(async () => {
      await identityApi.setSsoConnectionStatus(connection.id, status);
      onChanged();
    }, `Connection is now ${status}.`);
  }

  const oidc = connection.protocol === "oidc";

  return (
    <>
      <Section
        title={`${connection.name}`}
        description={
          oidc
            ? "OpenID Connect. The client secret is sealed at rest and is never returned by any endpoint."
            : "SAML 2.0. Assertions are verified against the certificates below, and only against those."
        }
        actions={
          <div className="section-actions">
            <ProtocolBadge protocol={connection.protocol} />
            <StatusBadge status={connection.status} />
            {connection.status === "active" ? (
              <button
                type="button"
                className="danger"
                disabled={submit.busy}
                onClick={() => void activate("disabled")}
              >
                Disable
              </button>
            ) : (
              <button
                type="button"
                className="primary"
                disabled={submit.busy}
                onClick={() => void activate("active")}
              >
                Activate
              </button>
            )}
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

        <InlineFacts
          facts={[
            { label: "Slug", value: <code>{connection.slug}</code> },
            { label: "Sign-in URL", value: <code>/auth/sso/{connection.slug}/start</code> },
            { label: "SP entity id", value: <code>{connection.sp_entity_id || <Dash />}</code> },
            { label: "Last login", value: connection.last_login_at ?? "never" },
          ]}
        />

        <div className="action-row">
          <button type="button" disabled={submit.busy} onClick={() => void runTest()}>
            {submit.busy ? "Checking…" : "Run configuration check"}
          </button>
          {connection.protocol === "saml" ? (
            <a
              className="button-link"
              href={`${identityApi.ssoMetadataUrl(connection.id)}`}
              target="_blank"
              rel="noreferrer"
            >
              Open SP metadata
            </a>
          ) : null}
        </div>

        {test ? (
          <div className={test.ok ? "notice" : "warn-banner"}>
            <strong>{test.ok ? "Check passed" : "Check failed"}</strong>
            <p>{test.detail}</p>
            <ul className="plain">
              <li>Discovery document: {test.discovered ? "read" : "not read"}</li>
              <li>Issuer: {test.issuer || <Dash />}</li>
              <li>Signing keys: {test.jwks_keys}</li>
              <li>Certificates: {test.certificates}</li>
            </ul>
            {test.warnings.map((warning) => (
              <p className="hint" key={warning}>
                {warning}
              </p>
            ))}
          </div>
        ) : null}
      </Section>

      <Section
        title={oidc ? "OpenID Connect settings" : "SAML settings"}
        description="Changes are saved together with the claim mapping below, and are a privileged action."
        actions={
          <div className="section-actions">
            <button
              type="button"
              className="primary"
              disabled={submit.busy}
              onClick={() => void save()}
            >
              {submit.busy ? "Saving…" : "Save"}
            </button>
          </div>
        }
      >
        {oidc ? (
          <>
            <TextField
              id="issuer"
              label="Issuer"
              value={String(value("issuer", connection.issuer))}
              onChange={(next) => setEdits({ ...edits, issuer: next })}
              placeholder="https://idp.example.com"
              hint="Must match the issuer the provider publishes; a document naming a different one is refused."
            />
            <TextField
              id="discovery"
              label="Discovery URL"
              value={String(value("discovery_url", connection.discovery_url))}
              onChange={(next) => setEdits({ ...edits, discovery_url: next })}
              placeholder="https://idp.example.com/.well-known/openid-configuration"
              hint="Must be https. Leave empty to derive it from the issuer."
            />
            <TextField
              id="client-id"
              label="Client ID"
              value={String(value("client_id", connection.client_id))}
              onChange={(next) => setEdits({ ...edits, client_id: next })}
            />
            <TextField
              id="client-secret"
              label="Client secret"
              type="password"
              value={String(value("client_secret", "") ?? "")}
              onChange={(next) => setEdits({ ...edits, client_secret: next })}
              hint={
                connection.has_client_secret
                  ? "A secret is already stored (sealed). Leave empty to keep it."
                  : "Required for the authorization-code exchange."
              }
            />
            <TextField
              id="scopes"
              label="Scopes"
              value={String(value("scopes", connection.scopes))}
              onChange={(next) => setEdits({ ...edits, scopes: next })}
              placeholder="openid email profile"
            />
            <TextField
              id="redirect"
              label="Redirect URI"
              value={String(value("redirect_uri", connection.redirect_uri))}
              onChange={(next) => setEdits({ ...edits, redirect_uri: next })}
              hint="Registered at the provider. Leave empty to derive it from this deployment's base URL."
            />
            <CheckField
              id="pkce"
              label="Use PKCE (S256)"
              checked={Boolean(value("use_pkce", connection.use_pkce))}
              onChange={(on) => setEdits({ ...edits, use_pkce: on })}
              hint="The verifier is sealed at rest and only decrypts to complete the exchange."
            />
          </>
        ) : (
          <>
            <TextField
              id="idp-entity"
              label="IdP entity id"
              value={String(value("idp_entity_id", connection.idp_entity_id))}
              onChange={(next) => setEdits({ ...edits, idp_entity_id: next })}
              hint="The assertion's issuer must equal this, exactly."
            />
            <TextField
              id="idp-sso"
              label="IdP sign-on URL"
              value={String(value("idp_sso_url", connection.idp_sso_url))}
              onChange={(next) => setEdits({ ...edits, idp_sso_url: next })}
            />
            <TextField
              id="idp-slo"
              label="IdP sign-out URL"
              value={String(value("idp_slo_url", connection.idp_slo_url))}
              onChange={(next) => setEdits({ ...edits, idp_slo_url: next })}
              hint="Optional. A sign-out request never ends our sessions by itself."
            />
            <TextField
              id="acs"
              label="ACS URL"
              value={String(value("acs_url", connection.acs_url))}
              onChange={(next) => setEdits({ ...edits, acs_url: next })}
              hint="Where the IdP posts the assertion. Leave empty to derive it."
            />
            <TextField
              id="name-id"
              label="Name-identifier format"
              value={String(value("name_id_format", ""))}
              onChange={(next) => setEdits({ ...edits, name_id_format: next })}
              placeholder="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
              hint="Optional. When set, an assertion using another format is refused."
            />
            <CheckField
              id="signed"
              label="Require an unsigned assertion to be refused"
              checked={Boolean(
                value("require_signed_assertions", connection.require_signed_assertions),
              )}
              onChange={(on) => setEdits({ ...edits, require_signed_assertions: on })}
              hint="Leave on. A signed envelope must not be able to protect a body somebody swapped."
            />
            <TextAreaField
              id="idp-metadata"
              label="IdP metadata XML"
              value={String(value("idp_metadata", ""))}
              onChange={(next) => setEdits({ ...edits, idp_metadata: next })}
              hint="Optional. Paste the provider's metadata document and it is read for the entity id, URLs and certificate."
            />
          </>
        )}

        <h3>Claims and provisioning</h3>
        <TextField
          id="subject-claim"
          label="Subject claim"
          value={String(value("subject_claim", connection.subject_claim))}
          onChange={(next) => setEdits({ ...edits, subject_claim: next })}
          hint="The stable identifier for a person. Changing it can create duplicate users."
        />
        <TextField
          id="email-claim"
          label="Email claim"
          value={String(value("email_claim", connection.email_claim))}
          onChange={(next) => setEdits({ ...edits, email_claim: next })}
        />
        <TextField
          id="name-claim"
          label="Name claim"
          value={String(value("name_claim", connection.name_claim))}
          onChange={(next) => setEdits({ ...edits, name_claim: next })}
        />
        <TextField
          id="role-claim"
          label="Role claim"
          value={String(value("role_claim", connection.role_claim))}
          onChange={(next) => setEdits({ ...edits, role_claim: next })}
          placeholder="roles"
          hint="Only values present in the mapping below are honoured."
        />
        <TextField
          id="group-claim"
          label="Group claim"
          value={String(value("group_claim", connection.group_claim))}
          onChange={(next) => setEdits({ ...edits, group_claim: next })}
          placeholder="groups"
        />
        <CheckField
          id="jit"
          label="Create accounts at first sign-in"
          checked={Boolean(value("jit_enabled", connection.jit_enabled))}
          onChange={(on) => setEdits({ ...edits, jit_enabled: on })}
        />
        <CheckField
          id="linking"
          label="Allow linking to an existing account with the same address"
          checked={Boolean(
            value("allow_account_linking", connection.allow_account_linking),
          )}
          onChange={(on) => setEdits({ ...edits, allow_account_linking: on })}
          hint="Only ever within this workspace: an address belonging to another tenant is refused, not linked."
        />
        <CheckField
          id="verified"
          label="Require a verified email before creating or linking"
          checked={Boolean(
            value("require_verified_email", connection.require_verified_email),
          )}
          onChange={(on) => setEdits({ ...edits, require_verified_email: on })}
        />
        <CheckField
          id="deny-unmapped"
          label="Refuse a sign-in whose role is not mapped"
          checked={Boolean(
            value("deny_unmapped_roles", connection.deny_unmapped_roles),
          )}
          onChange={(on) => setEdits({ ...edits, deny_unmapped_roles: on })}
          hint="An existing user keeps the role they have. Without this, a new user gets the default role."
        />
        <TextAreaField
          id="role-map"
          label="Role mapping"
          value={roleMap}
          onChange={setRoleMap}
          placeholder={"idp-admin=admin\nidp-agent=agent"}
          hint="One per line, claimed-value=role. Valid targets only; an unknown role is refused when saving."
        />
        <TextAreaField
          id="group-map"
          label="Group mapping"
          value={groupMap}
          onChange={setGroupMap}
          placeholder={"Call Centre=agent"}
          hint="Used after the role mapping, so a role mapping with the same name wins."
        />
        <p className="hint">
          Default role for a new account: <strong>{connection.default_role}</strong>.
        </p>
      </Section>

      {!oidc ? (
        <Section
          title="Signing certificates"
          description="An assertion is accepted only when its signature matches one of these, by fingerprint."
        >
          {certificates.error ? (
            <p className="error-banner">{certificates.error}</p>
          ) : null}

          <TextAreaField
            id="certificate"
            label="Add a certificate"
            value={cert}
            onChange={setCert}
            rows={6}
            hint="A PEM block, a bare base64 body, or a whole metadata document — the base64 is extracted. Re-uploading the same certificate is idempotent."
          />
          <button
            type="button"
            className="primary"
            disabled={submit.busy || cert.trim().length < 32}
            onClick={() => void addCertificate()}
          >
            {submit.busy ? "Adding…" : "Add certificate"}
          </button>

          {certificates.loading ? (
            <p className="muted">Loading certificates…</p>
          ) : (certificates.data ?? []).length === 0 ? (
            <div className="empty">
              No certificate yet, so nothing can be verified.
            </div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Fingerprint</th>
                    <th>Subject</th>
                    <th>Valid until</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {(certificates.data ?? []).map((row) => (
                    <tr key={row.id}>
                      <td>
                        <code>{row.fingerprint.slice(0, 24)}…</code>
                      </td>
                      <td>{row.subject || <Dash />}</td>
                      <td>{row.not_after ?? <Dash />}</td>
                      <td>
                        <StatusBadge status={row.status} />
                      </td>
                      <td className="row-actions">
                        {row.status === "active" ? (
                          <DangerAction
                            label="Retire"
                            confirmLabel="Retire certificate"
                            question="Retiring the last usable certificate of an active connection is refused, because it would lock everyone out."
                            onConfirm={() => void retireCertificate(row.id)}
                            busy={submit.busy}
                          />
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="hint">
            Rotation: add the new certificate (both are valid), roll the provider
            over, confirm sign-ins work, then retire the old one.
          </p>
        </Section>
      ) : null}
    </>
  );
}
