"use client";

import { useEffect, useState } from "react";
import { ReauthPrompt, ResultBanner } from "@/components/identity/actions";
import { CheckField, Section, TextField } from "@/components/identity/fields";
import { useSubmit } from "@/components/identity/use-identity";
import { describeDuration, identityApi } from "@/lib/identity";
import type {
  IdentityFeatures,
  IdentityPolicy,
  IdentityPolicyPatch,
} from "@/lib/identity-types";

/**
 * The tenant's identity policy.
 *
 * Every control here maps to one field of `identity_policies`, and the panel
 * deliberately does not invent a second vocabulary for it. Two rules are worth
 * knowing while using it:
 *
 * * Requiring single sign-on (or turning password sign-in off) is refused by
 *   the server unless this tenant already has an *active* SSO connection —
 *   otherwise the change would lock everyone out of a workspace nobody can get
 *   into.
 * * Every change is a privileged action, so a stale session answers 428 and the
 *   panel asks for a fresh proof rather than throwing the edit away.
 */
export default function PolicyPanel({
  policy,
  features,
  onChanged,
}: {
  policy: IdentityPolicy;
  features: IdentityFeatures;
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [draft, setDraft] = useState<IdentityPolicy>(policy);

  useEffect(() => {
    setDraft(policy);
  }, [policy]);

  function patch(change: Partial<IdentityPolicy>) {
    setDraft((current) => ({ ...current, ...change }));
  }

  function payload(): IdentityPolicyPatch {
    const fields: (keyof IdentityPolicy)[] = [
      "mfa_required",
      "mfa_required_for_admins",
      "privileged_reauth_required",
      "privileged_reauth_minutes",
      "sso_required",
      "password_login_allowed",
      "api_keys_allowed",
      "service_accounts_allowed",
      "scim_enabled",
      "jit_provisioning_allowed",
      "session_idle_minutes",
      "session_max_active",
      "refresh_token_days",
      "allowed_email_domains",
    ];
    const change: Record<string, unknown> = {};
    for (const field of fields) {
      if (draft[field] !== policy[field]) change[field] = draft[field];
    }
    return change as IdentityPolicyPatch;
  }

  const changed = Object.keys(payload()).length > 0;

  async function save() {
    const change = payload();
    if (Object.keys(change).length === 0) return;
    const ok = await submit.run(async () => {
      await identityApi.updatePolicy(change);
      onChanged();
    }, "Workspace identity policy updated.");
    if (!ok) {
      // Keep the operator's edit on screen: throwing it away on a 428 or a
      // refusal would mean retyping everything to fix one field.
      setDraft((current) => ({ ...current }));
    }
  }

  return (
    <Section
      title="Workspace policy"
      description={
        policy.is_default
          ? "Nothing has been configured for this workspace yet, so these are the defaults."
          : "These rules apply to everyone in this workspace, unless a person has an override."
      }
      actions={
        <div className="section-actions">
          <button
            type="button"
            className="primary"
            disabled={!changed || submit.busy}
            onClick={() => void save()}
          >
            {submit.busy ? "Saving…" : "Save changes"}
          </button>
        </div>
      }
    >
      <ResultBanner error={submit.error} notice={submit.notice} />

      {submit.needsReauth ? (
        <ReauthPrompt
          busy={submit.busy}
          canUseCode={features.mfa_enabled}
          canUsePassword={features.password_login_allowed}
          onReauthenticate={submit.reauthenticate}
        />
      ) : null}

      <h3>Second factor</h3>
      <CheckField
        id="mfa-required"
        label="Require two-factor authentication for everyone"
        checked={draft.mfa_required}
        disabled={!features.mfa_enabled}
        onChange={(value) => patch({ mfa_required: value })}
        hint={
          features.mfa_enabled
            ? "Anyone without a factor is asked to enrol at the next sign-in. They keep working until they do."
            : "MFA is switched off for this deployment."
        }
      />
      <CheckField
        id="mfa-admins"
        label="Require it for administrators"
        checked={draft.mfa_required_for_admins}
        disabled={!features.mfa_enabled}
        onChange={(value) => patch({ mfa_required_for_admins: value })}
        hint="Owners and administrators. Recommended: these accounts can change how everybody signs in."
      />
      <CheckField
        id="reauth-required"
        label="Require a fresh proof of presence for privileged changes"
        checked={draft.privileged_reauth_required}
        onChange={(value) => patch({ privileged_reauth_required: value })}
        hint="Changing SSO, disabling a factor, rotating a credential, or lifting an emergency stop asks for a code or a password first."
      />
      <TextField
        id="reauth-minutes"
        label="How long that proof stays valid (minutes)"
        type="number"
        value={String(draft.privileged_reauth_minutes)}
        onChange={(value) =>
          patch({ privileged_reauth_minutes: Number(value) || 0 })
        }
        hint={`Currently ${describeDuration(draft.privileged_reauth_minutes, "minutes")}.`}
      />

      <h3>How people sign in</h3>
      <CheckField
        id="sso-required"
        label="Require single sign-on"
        checked={draft.sso_required}
        disabled={!features.sso_enabled}
        onChange={(value) => patch({ sso_required: value })}
        hint={
          features.sso_enabled
            ? "Refused until an SSO connection is active, because until then nobody could sign in."
            : "SSO is switched off for this deployment."
        }
      />
      <CheckField
        id="password-allowed"
        label="Allow password sign-in"
        checked={draft.password_login_allowed}
        onChange={(value) => patch({ password_login_allowed: value })}
        hint="Turning this off is refused unless SSO is active. Service accounts and API keys are unaffected."
      />
      <CheckField
        id="jit"
        label="Create accounts on first federated sign-in (just in time)"
        checked={draft.jit_provisioning_allowed}
        disabled={!features.sso_enabled}
        onChange={(value) => patch({ jit_provisioning_allowed: value })}
      />
      <TextField
        id="allowed-domains"
        label="Restrict sign-in to these email domains"
        value={draft.allowed_email_domains.join(", ")}
        onChange={(value) =>
          patch({
            allowed_email_domains: value
              .split(",")
              .map((entry) => entry.trim().toLowerCase())
              .filter(Boolean),
          })
        }
        placeholder="acme.example.com, acme-europe.example.com"
        hint="Comma separated. Empty means any address may sign in, subject to the rules above."
      />

      <h3>Machine credentials</h3>
      <CheckField
        id="api-keys"
        label="Allow API keys"
        checked={draft.api_keys_allowed}
        onChange={(value) => patch({ api_keys_allowed: value })}
        hint="Checked on every request, so turning this off stops existing keys immediately."
      />
      <CheckField
        id="service-accounts"
        label="Allow service accounts"
        checked={draft.service_accounts_allowed}
        onChange={(value) => patch({ service_accounts_allowed: value })}
        hint="Also checked per request, and never usable for privileged actions."
      />
      <CheckField
        id="scim"
        label="Allow SCIM provisioning"
        checked={draft.scim_enabled}
        onChange={(value) => patch({ scim_enabled: value })}
        hint="Turning this off stops an identity provider from creating, updating or deactivating people here."
      />

      <h3>Sessions</h3>
      <TextField
        id="idle"
        label="Sign out after this many idle minutes"
        type="number"
        value={String(draft.session_idle_minutes)}
        onChange={(value) => patch({ session_idle_minutes: Number(value) || 0 })}
        hint={`Currently ${describeDuration(draft.session_idle_minutes, "minutes")}.`}
      />
      <TextField
        id="max-active"
        label="Maximum open sessions per person"
        type="number"
        value={String(draft.session_max_active)}
        onChange={(value) => patch({ session_max_active: Number(value) || 0 })}
        hint="The oldest session is closed when the limit is reached, so nobody is locked out by a forgotten device."
      />
      <TextField
        id="refresh-days"
        label="Absolute session lifetime (days)"
        type="number"
        value={String(draft.refresh_token_days)}
        onChange={(value) => patch({ refresh_token_days: Number(value) || 0 })}
        hint={`Currently ${describeDuration(draft.refresh_token_days, "days")}. A session never outlives this.`}
      />
    </Section>
  );
}
