// Types for the enterprise identity API.
//
// Every shape here is copied from the backend's own response models (see
// `app/api/identity_routes.py`, `mfa_routes.py`, `session_routes.py`,
// `machine_routes.py`, `domain_routes.py`, `sso_routes.py`, `scim_routes.py`),
// so a field that the server does not send cannot be invented here without
// being noticed in review. Optional fields are optional because the server
// defaults them, not because they might be missing by accident.

export interface IdentityFeatures {
  mfa_enabled: boolean;
  sso_enabled: boolean;
  scim_enabled: boolean;
  password_login_allowed: boolean;
  sso_required: boolean;
  secrets_configured: boolean;
  mfa_issuer: string;
  mfa_digits: number;
  mfa_period_seconds: number;
}

export interface IdentityPolicy {
  identity_policy_id: string | null;
  is_default: boolean;
  mfa_required: boolean;
  mfa_required_for_admins: boolean;
  privileged_reauth_required: boolean;
  privileged_reauth_minutes: number;
  sso_required: boolean;
  password_login_allowed: boolean;
  api_keys_allowed: boolean;
  service_accounts_allowed: boolean;
  scim_enabled: boolean;
  jit_provisioning_allowed: boolean;
  session_idle_minutes: number;
  session_max_active: number;
  refresh_token_days: number;
  allowed_email_domains: string[];
}

/** The subset of a policy the dashboard may change. */
export type IdentityPolicyPatch = Partial<
  Omit<IdentityPolicy, "identity_policy_id" | "is_default">
>;

export interface MFAStatus {
  enrolled: boolean;
  pending: boolean;
  factor_id?: string | null;
  confirmed_at?: string | null;
  last_used_at?: string | null;
  recovery_codes_remaining: number;
  recovery_codes_total: number;
  recovery_codes_low?: boolean;
  required_by_policy: boolean;
  required_for_admins?: boolean;
  enabled_on_deployment?: boolean;
  issuer?: string;
  digits?: number;
  period_seconds?: number;
}

export interface MFASessionState {
  session_id: string | null;
  auth_method: string;
  mfa_verified: boolean;
  mfa_verified_at: string | null;
  password_confirmed_at: string | null;
  created_at: string | null;
  idle_expires_at: string | null;
  expires_at: string | null;
  device: string;
}

export interface ReauthState {
  required: boolean;
  fresh_minutes: number;
  has_factor: boolean;
  password_set: boolean;
}

export interface IdentityStatus {
  features: IdentityFeatures;
  policy: IdentityPolicy;
  mfa: MFAStatus;
  session: MFASessionState;
  email_verified: boolean;
  reauth: ReauthState;
}

export interface IdentityConfig {
  mfa_supported: boolean;
  sso_supported: boolean;
  password_reset_supported: boolean;
  password_min_length: number;
  delivery_configured: boolean;
}

export interface IdentityEvent {
  id: string;
  action: string;
  actor_user_id: string | null;
  actor_email: string;
  target_user_id: string | null;
  ip_address: string;
  created_at: string | null;
  detail: Record<string, unknown>;
}

export interface ReauthOut {
  ok: boolean;
  method: string;
}

// ------------------------------------------------------------------ MFA ---

export interface EnrollOut {
  factor_id: string;
  secret: string;
  provisioning_uri: string;
  digits: number;
  period_seconds: number;
  hint: string;
}

export interface RecoveryCodesOut {
  recovery_codes: string[];
  warning: string;
}

/**
 * What `/auth/login` answers when a second factor is owed (HTTP 202). Defined
 * with the other auth-flow types because `api.login` returns it; re-exported
 * here so identity code has one import site.
 */
export type { MFAChallengeResponse as MFAChallenge } from "./types";

export interface StepUpOut {
  ok: boolean;
  mfa_verified: boolean;
  used_recovery_code: boolean;
}

/**
 * Mirrors app.api.mfa_routes.ChallengeOut: a single-use, session-bound handle
 * for a step-up. It is what `POST /api/mfa/verify` should be given together
 * with the code, and it replaces any earlier open challenge for the user.
 */
export interface MFAChallengeToken {
  challenge_token: string;
  expires_at: string;
  purpose: string;
}

// -------------------------------------------------------------- sessions ---

export interface SessionRow {
  id: string;
  device: string;
  user_agent: string;
  ip_address: string;
  auth_method: string;
  mfa_verified: boolean;
  current: boolean;
  created_at: string;
  last_seen_at: string;
  idle_expires_at: string;
  expires_at: string;
}

export interface SessionLimits {
  idle_minutes: number;
  max_active: number;
  refresh_days: number;
  absolute_expires_at: string;
}

export interface SessionList {
  sessions: SessionRow[];
  current_session_id: string | null;
  limits: SessionLimits;
}

export interface RevokeOthersOut {
  revoked: number;
}

// ------------------------------------------------------- password + email ---

export interface ResetRequestOut {
  status: string;
  message: string;
  development_token?: string | null;
}

export interface ResetConfirmOut {
  status: string;
  revoked_sessions: number;
}

export interface VerifyRequestOut {
  status: string;
  message: string;
  development_token?: string | null;
}

export interface VerifyConfirmOut {
  status: string;
}

// ------------------------------------------------------------- API keys ---

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked_reason: string;
  owner_user_id: string | null;
  service_account_id: string | null;
  rotated_from_id: string | null;
}

export interface ApiKeyCreated extends ApiKey {
  /** Present exactly once, in the response that created the key. */
  secret: string;
  warning: string;
}

export interface ApiKeyIn {
  name: string;
  scopes: string[];
  expires_in_days?: number | null;
}

/** `GET /api/api-keys/scopes`: what this caller may grant, and every value. */
export interface ScopeCatalogue {
  grantable: string[];
  all_scopes: string[];
}

// ------------------------------------------------------ service accounts ---

export interface ServiceAccount {
  id: string;
  name: string;
  description: string;
  scopes: string[];
  scope_summary: string;
  enabled: boolean;
  emergency_disabled: boolean;
  disabled_reason: string;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
  owner_user_id: string | null;
  credential_count: number;
}

export interface ServiceAccountIn {
  name: string;
  description?: string;
  scopes: string[];
  expires_in_days?: number | null;
}

export interface ServiceAccountUpdateIn {
  name?: string;
  description?: string;
  scopes?: string[];
  owner_user_id?: string;
  expires_in_days?: number | null;
}

export interface MachineCredential {
  id: string;
  label: string;
  prefix: string;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked_reason: string;
}

export interface MachineCredentialCreated extends MachineCredential {
  secret: string;
  warning: string;
}

export interface CredentialIn {
  label?: string;
  expires_in_days?: number | null;
}

/** `reason` is required by the emergency endpoints and optional elsewhere. */
export interface ReasonIn {
  reason?: string;
}

// --------------------------------------------------------------- domains ---

export interface EnterpriseDomain {
  id: string;
  domain: string;
  verified: boolean;
  verified_at: string | null;
  enforcement: "off" | "warn" | "require_sso" | string;
  block_password_login: boolean;
  sso_connection_id: string | null;
  failed_attempts: number;
  created_at: string | null;
  verification_record_name: string;
  verification_status: string;
  /** Only present on the create/challenge responses: the token is shown once. */
  verification_record_value?: string;
}

export interface DomainVerifyOut {
  verified: boolean;
  status: string;
  detail: string;
  record_name: string;
  records_seen: number;
}

export interface DomainUpdateIn {
  enforcement?: string;
  block_password_login?: boolean;
  sso_connection_id?: string | null;
}

// ------------------------------------------------------------------- SSO ---

export interface SSOConnection {
  id: string;
  name: string;
  slug: string;
  protocol: "oidc" | "saml" | string;
  status: "draft" | "active" | "disabled" | string;
  issuer: string;
  discovery_url: string;
  client_id: string;
  /** The secret itself is never returned; this says whether one is set. */
  has_client_secret: boolean;
  scopes: string;
  use_pkce: boolean;
  idp_entity_id: string;
  idp_sso_url: string;
  idp_slo_url: string;
  sp_entity_id: string;
  acs_url: string;
  redirect_uri: string;
  require_signed_assertions: boolean;
  email_claim: string;
  name_claim: string;
  subject_claim: string;
  group_claim: string;
  role_claim: string;
  default_role: string;
  group_mapping: Record<string, string>;
  role_mapping: Record<string, string>;
  deny_unmapped_roles: boolean;
  jit_enabled: boolean;
  allow_account_linking: boolean;
  require_verified_email: boolean;
  active_certificates: number;
  created_at: string | null;
  last_login_at: string | null;
  metadata_url: string;
}

export interface SSOConnectionIn {
  name: string;
  slug?: string;
  protocol?: string;
  issuer?: string;
  discovery_url?: string;
  client_id?: string;
  client_secret?: string;
  scopes?: string;
  use_pkce?: boolean;
  redirect_uri?: string;
  idp_entity_id?: string;
  idp_sso_url?: string;
  idp_slo_url?: string;
  idp_metadata?: string;
  sp_entity_id?: string;
  acs_url?: string;
  require_signed_assertions?: boolean;
  name_id_format?: string;
  email_claim?: string;
  name_claim?: string;
  subject_claim?: string;
  group_claim?: string;
  role_claim?: string;
  default_role?: string;
  jit_enabled?: boolean;
  allow_account_linking?: boolean;
  require_verified_email?: boolean;
  deny_unmapped_roles?: boolean;
}

export interface SSOCertificate {
  id: string;
  fingerprint: string;
  subject: string;
  issuer: string;
  status: string;
  not_before: string | null;
  not_after: string | null;
  created_at: string | null;
  retired_at: string | null;
}

export interface SSOMappingIn {
  group_mapping?: Record<string, string>;
  role_mapping?: Record<string, string>;
  default_role?: string;
  deny_unmapped_roles?: boolean;
}

export interface SSOTestOut {
  ok: boolean;
  detail: string;
  discovered: boolean;
  issuer: string;
  jwks_keys: number;
  certificates: number;
  warnings: string[];
}

export interface SSOAttempt {
  id: string;
  connection_id: string;
  protocol: string;
  kind: string;
  outcome: string;
  failure_reason: string;
  ip_address: string;
  created_at: string | null;
  consumed_at: string | null;
}

export interface SSODiscoverOut {
  sso: boolean;
  connection_id: string | null;
  slug: string | null;
  protocol: string | null;
  name: string | null;
  sso_required: boolean;
  password_allowed: boolean;
}

// ------------------------------------------------------------------ SCIM ---

export interface ScimCredential {
  id: string;
  label: string;
  prefix: string;
  scopes: string[];
  connection_id: string | null;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export interface ScimCredentialCreated extends ScimCredential {
  token: string;
  warning: string;
}

export interface ScimCredentialIn {
  label?: string;
  connection_id?: string | null;
  scopes?: string[];
  expires_in_days?: number | null;
}

// ------------------------------------------------------ API error bodies ---

/**
 * The backend answers identity failures with `{code, message, reason}`.
 * `code` names the class of refusal and `reason` names the specific rule, so a
 * UI can point at the thing that has to change without parsing prose.
 */
export interface IdentityErrorBody {
  code: string;
  message: string;
  reason?: string;
}
