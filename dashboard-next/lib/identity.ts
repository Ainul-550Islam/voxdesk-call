// Identity API client.
//
// One module for the whole enterprise identity surface: policy, MFA, sessions,
// password reset, API keys, service accounts, domains, SSO connections and SCIM
// credentials. It reuses the transport in `lib/api.ts` (Bearer token, refresh
// on 401, `detail`-shaped errors), so an expired access token behaves the same
// here as anywhere else in the dashboard.
//
// Two rules this module holds to:
//
// * A secret is returned by exactly one call — the one that created it — and is
//   typed separately (`secret`, `token`) so a component cannot accidentally
//   render a list row as if it carried a credential.
// * Nothing here builds a URL by concatenation of user input without
//   `encodeURIComponent`; the values come from server responses, but a slug or
//   a filter is still attacker-influenced data.

import { ApiError, request } from "./api";
import type {
  ApiKey,
  ApiKeyCreated,
  ApiKeyIn,
  CredentialIn,
  DomainUpdateIn,
  DomainVerifyOut,
  EnterpriseDomain,
  EnrollOut,
  IdentityConfig,
  IdentityEvent,
  IdentityPolicy,
  IdentityPolicyPatch,
  IdentityStatus,
  MachineCredential,
  MachineCredentialCreated,
  MFAStatus,
  MFAChallenge,
  MFAChallengeToken,
  ReauthOut,
  ReasonIn,
  RecoveryCodesOut,
  ResetConfirmOut,
  ResetRequestOut,
  RevokeOthersOut,
  SSOAttempt,
  SSOCertificate,
  SSOConnection,
  SSOConnectionIn,
  SSODiscoverOut,
  SSOMappingIn,
  SSOTestOut,
  ScopeCatalogue,
  ScimCredential,
  ScimCredentialCreated,
  ScimCredentialIn,
  ServiceAccount,
  ServiceAccountIn,
  ServiceAccountUpdateIn,
  SessionList,
  StepUpOut,
  VerifyConfirmOut,
  VerifyRequestOut,
} from "./identity-types";
import type { TokenResponse } from "./types";

function segment(value: string): string {
  return encodeURIComponent(value);
}

export const identityApi = {
  // ---------------------------------------------------------- overview ---

  status(): Promise<IdentityStatus> {
    return request<IdentityStatus>("/api/identity/status");
  },

  policy(): Promise<IdentityPolicy> {
    return request<IdentityPolicy>("/api/identity/policy");
  },

  updatePolicy(patch: IdentityPolicyPatch): Promise<IdentityPolicy> {
    return request<IdentityPolicy>("/api/identity/policy", {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
  },

  events(limit = 50): Promise<IdentityEvent[]> {
    return request<IdentityEvent[]>(`/api/identity/events?limit=${limit}`);
  },

  /**
   * Confirm presence for a privileged action. Either a password or a second
   * factor; the server rate limits both and answers 428 when something more is
   * needed than the caller supplied.
   */
  reauth(body: { password?: string; code?: string }): Promise<ReauthOut> {
    return request<ReauthOut>("/api/identity/reauth", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  /** Public: what this deployment supports, for the sign-in and reset pages. */
  config(): Promise<IdentityConfig> {
    return request<IdentityConfig>("/auth/identity-config");
  },

  // --------------------------------------------------------------- MFA ---

  mfaStatus(): Promise<MFAStatus> {
    return request<MFAStatus>("/api/mfa/status");
  },

  /** Begin enrollment. The seed is returned once and never again. */
  mfaEnroll(): Promise<EnrollOut> {
    return request<EnrollOut>("/api/mfa/enroll", { method: "POST" });
  },

  /** Confirming enrollment returns the recovery codes — shown once. */
  mfaEnrollConfirm(code: string): Promise<RecoveryCodesOut> {
    return request<RecoveryCodesOut>("/api/mfa/enroll/confirm", {
      method: "POST",
      body: JSON.stringify({ code }),
    });
  },

  /**
   * Finish a login that stopped at the second factor. `api.login` returns an
   * `MFAChallenge` (HTTP 202) instead of tokens when one is owed.
   */
  loginMfaVerify(challenge: string, code: string): Promise<TokenResponse> {
    return request<TokenResponse>("/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ challenge, code }),
      // A wrong code must not look like an expired session, so no refresh loop.
    });
  },

  /** A fresh, session-bound step-up challenge. */
  mfaChallenge(): Promise<MFAChallengeToken> {
    return request<MFAChallengeToken>("/api/mfa/challenge", { method: "POST" });
  },

  /**
   * Answer a step-up. Pass the challenge token whenever there is one: the
   * server then resolves it through the challenge row (single use, bound to
   * this session, counted against the per-challenge failure ceiling). A bare
   * code still works but is only rate limited per user.
   */
  mfaStepUp(code: string, challenge?: string): Promise<StepUpOut> {
    return request<StepUpOut>("/api/mfa/verify", {
      method: "POST",
      body: JSON.stringify(challenge ? { code, challenge } : { code }),
    });
  },

  /**
   * Turn the caller's factor off. The proof of presence is carried by the
   * session (a privileged action), so there is no body: the caller must first
   * satisfy a 428 with `identityApi.reauth`, then retry.
   */
  mfaDisable(): Promise<void> {
    return request<void>("/api/mfa/disable", { method: "POST" });
  },

  /** Replace the caller's recovery codes. Previous codes stop working. */
  regenerateRecoveryCodes(): Promise<RecoveryCodesOut> {
    return request<RecoveryCodesOut>("/api/mfa/recovery-codes/regenerate", {
      method: "POST",
    });
  },

  userMfaStatus(userId: string): Promise<MFAStatus> {
    return request<MFAStatus>(`/api/mfa/users/${segment(userId)}/status`);
  },

  resetUserMfa(userId: string): Promise<void> {
    return request<void>(`/api/mfa/users/${segment(userId)}/reset`, {
      method: "POST",
    });
  },

  // ---------------------------------------------------------- sessions ---

  sessions(): Promise<SessionList> {
    return request<SessionList>("/api/sessions");
  },

  renameSession(sessionId: string, label: string): Promise<SessionList> {
    return request<SessionList>(`/api/sessions/${segment(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ label }),
    });
  },

  revokeSession(sessionId: string): Promise<void> {
    return request<void>(`/api/sessions/${segment(sessionId)}`, {
      method: "DELETE",
    });
  },

  revokeOtherSessions(): Promise<RevokeOthersOut> {
    return request<RevokeOthersOut>("/api/sessions/revoke-others", {
      method: "POST",
    });
  },

  revokeAllSessions(): Promise<RevokeOthersOut> {
    return request<RevokeOthersOut>("/api/sessions", { method: "DELETE" });
  },

  // ------------------------------------------------- password + email ---

  requestPasswordReset(email: string): Promise<ResetRequestOut> {
    return request<ResetRequestOut>("/auth/password-reset/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
  },

  confirmPasswordReset(
    token: string,
    newPassword: string,
  ): Promise<ResetConfirmOut> {
    return request<ResetConfirmOut>("/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({ token, new_password: newPassword }),
    });
  },

  requestEmailVerification(): Promise<VerifyRequestOut> {
    return request<VerifyRequestOut>("/auth/email-verification/request", {
      method: "POST",
    });
  },

  confirmEmailVerification(token: string): Promise<VerifyConfirmOut> {
    return request<VerifyConfirmOut>("/auth/email-verification/confirm", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
  },

  // ---------------------------------------------------------- API keys ---

  apiKeys(options: { includeRevoked?: boolean; mineOnly?: boolean } = {}) {
    const params = new URLSearchParams();
    if (options.includeRevoked) params.set("include_revoked", "true");
    if (options.mineOnly) params.set("mine_only", "true");
    const query = params.toString();
    return request<ApiKey[]>(`/api/api-keys${query ? `?${query}` : ""}`);
  },

  apiKeyScopes(): Promise<ScopeCatalogue> {
    return request<ScopeCatalogue>("/api/api-keys/scopes");
  },

  createApiKey(body: ApiKeyIn): Promise<ApiKeyCreated> {
    return request<ApiKeyCreated>("/api/api-keys", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  rotateApiKey(keyId: string): Promise<ApiKeyCreated> {
    return request<ApiKeyCreated>(
      `/api/api-keys/${segment(keyId)}/rotate`,
      { method: "POST" },
    );
  },

  revokeApiKey(keyId: string): Promise<void> {
    return request<void>(`/api/api-keys/${segment(keyId)}`, { method: "DELETE" });
  },

  // --------------------------------------------------- service accounts ---

  serviceAccounts(): Promise<ServiceAccount[]> {
    return request<ServiceAccount[]>("/api/service-accounts");
  },

  serviceAccount(accountId: string): Promise<ServiceAccount> {
    return request<ServiceAccount>(
      `/api/service-accounts/${segment(accountId)}`,
    );
  },

  createServiceAccount(body: ServiceAccountIn): Promise<ServiceAccount> {
    return request<ServiceAccount>("/api/service-accounts", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  updateServiceAccount(
    accountId: string,
    body: ServiceAccountUpdateIn,
  ): Promise<ServiceAccount> {
    return request<ServiceAccount>(
      `/api/service-accounts/${segment(accountId)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
  },

  enableServiceAccount(
    accountId: string,
    body: ReasonIn = {},
  ): Promise<ServiceAccount> {
    return request<ServiceAccount>(
      `/api/service-accounts/${segment(accountId)}/enable`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  disableServiceAccount(
    accountId: string,
    body: ReasonIn = {},
  ): Promise<ServiceAccount> {
    return request<ServiceAccount>(
      `/api/service-accounts/${segment(accountId)}/disable`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  /** Trip the platform stop. Any administrator may do this. */
  emergencyDisableServiceAccount(
    accountId: string,
    reason: string,
  ): Promise<ServiceAccount> {
    return request<ServiceAccount>(
      `/api/service-accounts/${segment(accountId)}/emergency-disable`,
      { method: "POST", body: JSON.stringify({ reason }) },
    );
  },

  /**
   * Lift the platform stop. Owner-only, with a reason of at least 8
   * characters; an administrator gets 403 `owner_required`.
   */
  emergencyClearServiceAccount(
    accountId: string,
    reason: string,
  ): Promise<ServiceAccount> {
    return request<ServiceAccount>(
      `/api/service-accounts/${segment(accountId)}/emergency-clear`,
      { method: "POST", body: JSON.stringify({ reason }) },
    );
  },

  deleteServiceAccount(accountId: string): Promise<void> {
    return request<void>(`/api/service-accounts/${segment(accountId)}`, {
      method: "DELETE",
    });
  },

  serviceAccountCredentials(
    accountId: string,
  ): Promise<MachineCredential[]> {
    return request<MachineCredential[]>(
      `/api/service-accounts/${segment(accountId)}/credentials`,
    );
  },

  createServiceAccountCredential(
    accountId: string,
    body: CredentialIn,
  ): Promise<MachineCredentialCreated> {
    return request<MachineCredentialCreated>(
      `/api/service-accounts/${segment(accountId)}/credentials`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  rotateServiceAccountCredential(
    accountId: string,
    credentialId: string,
  ): Promise<MachineCredentialCreated> {
    return request<MachineCredentialCreated>(
      `/api/service-accounts/${segment(accountId)}/credentials/${segment(
        credentialId,
      )}/rotate`,
      { method: "POST" },
    );
  },

  revokeServiceAccountCredential(
    accountId: string,
    credentialId: string,
  ): Promise<void> {
    return request<void>(
      `/api/service-accounts/${segment(accountId)}/credentials/${segment(
        credentialId,
      )}`,
      { method: "DELETE" },
    );
  },

  serviceAccountKeys(accountId: string): Promise<ApiKey[]> {
    return request<ApiKey[]>(
      `/api/service-accounts/${segment(accountId)}/keys`,
    );
  },

  createServiceAccountKey(
    accountId: string,
    body: ApiKeyIn,
  ): Promise<ApiKeyCreated> {
    return request<ApiKeyCreated>(
      `/api/service-accounts/${segment(accountId)}/keys`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  // ----------------------------------------------------------- domains ---

  domains(): Promise<EnterpriseDomain[]> {
    return request<EnterpriseDomain[]>("/api/domains");
  },

  addDomain(domain: string): Promise<EnterpriseDomain> {
    return request<EnterpriseDomain>("/api/domains", {
      method: "POST",
      body: JSON.stringify({ domain }),
    });
  },

  domain(domainId: string): Promise<EnterpriseDomain> {
    return request<EnterpriseDomain>(`/api/domains/${segment(domainId)}`);
  },

  /** New DNS challenge. Supersedes any open one and shows the token once. */
  issueDomainChallenge(domainId: string): Promise<EnterpriseDomain> {
    return request<EnterpriseDomain>(
      `/api/domains/${segment(domainId)}/challenge`,
      { method: "POST" },
    );
  },

  verifyDomain(domainId: string): Promise<DomainVerifyOut> {
    return request<DomainVerifyOut>(
      `/api/domains/${segment(domainId)}/verify`,
      { method: "POST" },
    );
  },

  updateDomain(
    domainId: string,
    body: DomainUpdateIn,
  ): Promise<EnterpriseDomain> {
    return request<EnterpriseDomain>(`/api/domains/${segment(domainId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },

  removeDomain(domainId: string): Promise<void> {
    return request<void>(`/api/domains/${segment(domainId)}`, {
      method: "DELETE",
    });
  },

  // --------------------------------------------------------------- SSO ---

  ssoConnections(): Promise<SSOConnection[]> {
    return request<SSOConnection[]>("/api/sso/connections");
  },

  ssoConnection(connectionId: string): Promise<SSOConnection> {
    return request<SSOConnection>(
      `/api/sso/connections/${segment(connectionId)}`,
    );
  },

  createSsoConnection(body: SSOConnectionIn): Promise<SSOConnection> {
    return request<SSOConnection>("/api/sso/connections", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  updateSsoConnection(
    connectionId: string,
    body: Partial<SSOConnectionIn>,
  ): Promise<SSOConnection> {
    return request<SSOConnection>(
      `/api/sso/connections/${segment(connectionId)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
  },

  setSsoConnectionStatus(
    connectionId: string,
    status: "draft" | "active" | "disabled",
  ): Promise<SSOConnection> {
    return request<SSOConnection>(
      `/api/sso/connections/${segment(connectionId)}/status`,
      { method: "POST", body: JSON.stringify({ status }) },
    );
  },

  deleteSsoConnection(connectionId: string): Promise<void> {
    return request<void>(`/api/sso/connections/${segment(connectionId)}`, {
      method: "DELETE",
    });
  },

  ssoCertificates(connectionId: string): Promise<SSOCertificate[]> {
    return request<SSOCertificate[]>(
      `/api/sso/connections/${segment(connectionId)}/certificates`,
    );
  },

  addSsoCertificate(
    connectionId: string,
    certificate: string,
    makeActive = true,
  ): Promise<SSOCertificate> {
    return request<SSOCertificate>(
      `/api/sso/connections/${segment(connectionId)}/certificates`,
      { method: "POST", body: JSON.stringify({ certificate, make_active: makeActive }) },
    );
  },

  retireSsoCertificate(
    connectionId: string,
    certificateId: string,
  ): Promise<void> {
    return request<void>(
      `/api/sso/connections/${segment(connectionId)}/certificates/${segment(
        certificateId,
      )}`,
      { method: "DELETE" },
    );
  },

  setSsoMappings(
    connectionId: string,
    body: SSOMappingIn,
  ): Promise<SSOConnection> {
    return request<SSOConnection>(
      `/api/sso/connections/${segment(connectionId)}/mappings`,
      { method: "PUT", body: JSON.stringify(body) },
    );
  },

  testSsoConnection(connectionId: string): Promise<SSOTestOut> {
    return request<SSOTestOut>(
      `/api/sso/connections/${segment(connectionId)}/test`,
      { method: "POST" },
    );
  },

  /** The metadata URL is shown to an operator; the document itself is XML. */
  ssoMetadataUrl(connectionId: string): string {
    return `/api/sso/connections/${segment(connectionId)}/metadata`;
  },

  ssoAttempts(): Promise<SSOAttempt[]> {
    return request<SSOAttempt[]>("/api/sso/attempts");
  },

  /** Public: which IdP, if any, handles an address. */
  ssoDiscover(email: string): Promise<SSODiscoverOut> {
    return request<SSODiscoverOut>(
      `/auth/sso/discover?email=${segment(email)}`,
    );
  },

  /**
   * Begin a federated login. Returns the URL to send the browser to rather
   * than redirecting, so the caller decides when to leave the page.
   */
  ssoStart(slug: string): Promise<{
    authorization_url: string;
    state: string;
    connection_id: string;
    protocol: string;
  }> {
    return request(
      `/auth/sso/${segment(slug)}/start`,
      { method: "POST" },
      { retryOn401: false },
    );
  },

  // -------------------------------------------------------------- SCIM ---

  scimCredentials(): Promise<ScimCredential[]> {
    return request<ScimCredential[]>("/api/scim/credentials");
  },

  createScimCredential(
    body: ScimCredentialIn,
  ): Promise<ScimCredentialCreated> {
    return request<ScimCredentialCreated>("/api/scim/credentials", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  rotateScimCredential(
    credentialId: string,
  ): Promise<ScimCredentialCreated> {
    return request<ScimCredentialCreated>(
      `/api/scim/credentials/${segment(credentialId)}/rotate`,
      { method: "POST" },
    );
  },

  revokeScimCredential(credentialId: string): Promise<void> {
    return request<void>(`/api/scim/credentials/${segment(credentialId)}`, {
      method: "DELETE",
    });
  },

  /** The base URL to paste into an IdP's provisioning settings. */
  scimBaseUrl(connectionId: string | null): string {
    return `/scim/v2/${segment(connectionId ?? "default")}`;
  },
};

// ------------------------------------------------------------- helpers ---

/**
 * Where a secret can be presented, and where it cannot.
 *
 * A created key, credential or SCIM token is shown once. This narrows an
 * `unknown` error value to the field name a form should point at, so an
 * operator sees "the reason must be at least 8 characters" instead of a code.
 */
export function identityErrorReason(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return "request failed";
}

/** `require_sso` → `Require SSO`. Used for the enforcement picker and badges. */
export function enforcementLabel(value: string): string {
  const words = (value || "off").split(/[_\s-]+/).filter(Boolean);
  if (words.length === 0) return "Off";
  return words
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * How a policy value should read in a summary line.
 *
 * Minutes and days are the two units the policy uses, and "720 minutes" tells
 * an operator nothing useful, so durations are rendered in the largest unit
 * that divides them exactly.
 */
export function describeDuration(value: number, unit: "minutes" | "days"): string {
  if (!Number.isFinite(value) || value <= 0) return "—";
  if (unit === "days") {
    return value === 1 ? "1 day" : `${value} days`;
  }
  if (value % 1440 === 0) {
    const days = value / 1440;
    return days === 1 ? "1 day" : `${days} days`;
  }
  if (value % 60 === 0) {
    const hours = value / 60;
    return hours === 1 ? "1 hour" : `${hours} hours`;
  }
  return value === 1 ? "1 minute" : `${value} minutes`;
}

/**
 * `api_key:manage` → `Api Key Manage`, for the title on a scope chip.
 *
 * The chip itself shows the raw value, because an operator comparing a key
 * against a log needs the exact string; this is the reading aid beside it, so
 * the whole scope is spelled out rather than just the verb.
 */
export function scopeLabel(scope: string): string {
  return (scope || "")
    .split(/[:\s_-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * A slug a connection URL can carry.
 *
 * Mirrors the server's own normalisation (`normalized_slug` in
 * `app/auth/identity/sso/service.py`): lowercase, alphanumerics and single
 * hyphens, no leading or trailing hyphen, at most 60 characters. The server
 * normalises whatever it is sent anyway and replaces a too-short result, so
 * this is a preview rather than the authority — but a preview that disagreed
 * with the stored value would make the sign-in URL in the UI a lie.
 */
export function slugify(value: string): string {
  return (value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .slice(0, 60)
    .replace(/^-+|-+$/g, "");
}

/**
 * Whether a set of public form fields is complete enough to submit. Kept pure
 * so it can be tested without a DOM, and shared by the pages that use it.
 */
export function missingFields(
  values: Record<string, string>,
  required: string[],
): string[] {
  return required.filter((field) => !(values[field] || "").trim());
}

/** A short, stable label for a connection's protocol. */
export function protocolLabel(protocol: string): string {
  const upper = (protocol || "").toUpperCase();
  return upper === "OIDC" || upper === "SAML" ? upper : protocol || "—";
}

/** `active` → `ok`, `disabled` → `failed`, used for the status badge class. */
export function statusTone(
  status: string,
): "completed" | "failed" | "no_answer" {
  const value = (status || "").toLowerCase();
  if (["active", "verified", "succeeded", "ok", "enabled"].includes(value)) {
    return "completed";
  }
  if (["disabled", "failed", "revoked", "expired"].includes(value)) {
    return "failed";
  }
  return "no_answer";
}

