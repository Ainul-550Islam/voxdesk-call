"use client";

import { useState } from "react";
import {
  DangerAction,
  ReauthPrompt,
  ResultBanner,
  SecretOnce,
} from "@/components/identity/actions";
import {
  CheckField,
  InlineFacts,
  Section,
  TextField,
} from "@/components/identity/fields";
import { StatusBadge } from "@/components/identity/badges";
import { useLoader, useSubmit } from "@/components/identity/use-identity";
import { identityApi } from "@/lib/identity";
import type { EnrollOut, MFAStatus, RecoveryCodesOut } from "@/lib/identity-types";

/**
 * The caller's own second factor: enroll, confirm, review, replace codes, and
 * switch it off.
 *
 * The panel never asks for a code it does not need. Enrollment is the only
 * place a code is entered to *create* something; disabling and regenerating are
 * privileged actions, so the server decides whether a fresh proof is needed and
 * answers 428 when it is — that refusal is what `ReauthPrompt` is for.
 */
export default function MfaPanel({ status }: { status: MFAStatus | null }) {
  const submit = useSubmit();
  const [enrollment, setEnrollment] = useState<EnrollOut | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<RecoveryCodesOut | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);

  const state = useLoader<MFAStatus>(() => identityApi.mfaStatus(), {
    enabled: status === null,
  });
  const current = status ?? state.data;

  async function begin() {
    await submit.run(async () => {
      const started = await identityApi.mfaEnroll();
      setEnrollment(started);
      setCode("");
    }, "Enrollment started. Add the secret to your authenticator app.");
  }

  async function confirm() {
    await submit.run(async () => {
      const issued = await identityApi.mfaEnrollConfirm(code.trim());
      setCodes(issued);
      setEnrollment(null);
      setCode("");
      setAcknowledged(false);
    }, "Second factor enabled.");
  }

  async function disable() {
    await submit.run(async () => {
      await identityApi.mfaDisable();
      setCodes(null);
    }, "Second factor disabled. Your other sessions were signed out.");
  }

  async function regenerate() {
    await submit.run(async () => {
      const issued = await identityApi.regenerateRecoveryCodes();
      setCodes(issued);
      setAcknowledged(false);
    }, "New recovery codes issued. The previous set no longer works.");
  }

  if (!current) {
    return (
      <Section title="Two-factor authentication">
        {state.loading ? <p className="muted">Loading…</p> : null}
        {state.error ? <p className="error-banner">{state.error}</p> : null}
      </Section>
    );
  }

  return (
    <Section
      title="Two-factor authentication"
      description="A code from an authenticator app on top of your password. Required for administrators when the workspace policy says so."
      actions={<StatusBadge status={current.enrolled ? "active" : "disabled"} label={current.enrolled ? "Enabled" : "Off"} />}
    >
      <ResultBanner error={submit.error} notice={submit.notice} />

      <InlineFacts
        facts={[
          {
            label: "Status",
            value: current.enrolled
              ? "A factor is enrolled"
              : current.pending
                ? "Enrollment started, not confirmed"
                : "No factor",
          },
          {
            label: "Required",
            value: current.required_by_policy
              ? "Yes — by workspace policy"
              : "No — optional for this account",
          },
          {
            label: "Recovery codes",
            value: `${current.recovery_codes_remaining} of ${current.recovery_codes_total} unused`,
          },
          {
            label: "Last used",
            value: current.last_used_at ?? "never",
          },
        ]}
      />

      {submit.needsReauth ? (
        <ReauthPrompt
          busy={submit.busy}
          canUseCode={current.enrolled}
          canUsePassword
          onReauthenticate={submit.reauthenticate}
        />
      ) : null}

      {codes ? (
        <SecretOnce
          label="Recovery codes"
          secret={codes.recovery_codes.join("\n")}
          warning={codes.warning}
          onDismiss={() => {
            if (acknowledged) setCodes(null);
          }}
          extra={
            <CheckField
              id="codes-ack"
              label="I have saved these codes somewhere safe"
              checked={acknowledged}
              onChange={setAcknowledged}
              hint="Each code works once, as a replacement for the six-digit code."
            />
          }
        />
      ) : null}

      {enrollment ? (
        <div className="enroll">
          <p className="muted">
            Add this secret to your authenticator app, then enter the code it
            shows.
          </p>
          <code className="secret-value">{enrollment.secret}</code>
          <p className="hint">{enrollment.hint}</p>
          <details>
            <summary>Provisioning URI</summary>
            <code className="secret-value">{enrollment.provisioning_uri}</code>
          </details>
          <div className="inline-form">
            <TextField
              id="enroll-code"
              label="Code from your app"
              value={code}
              onChange={setCode}
              placeholder="123456"
              autoComplete="one-time-code"
              hint={`${enrollment.digits} digits, refreshed every ${enrollment.period_seconds} seconds.`}
            />
            <button
              type="button"
              className="primary"
              disabled={submit.busy || code.trim().length < 6}
              onClick={() => void confirm()}
            >
              {submit.busy ? "Confirming…" : "Confirm"}
            </button>
          </div>
        </div>
      ) : null}

      {!enrollment && !current.enrolled ? (
        <button
          type="button"
          className="primary"
          disabled={submit.busy}
          onClick={() => void begin()}
        >
          {submit.busy ? "Starting…" : "Enable two-factor"}
        </button>
      ) : null}

      {current.enrolled && !enrollment ? (
        <div className="action-row">
          <button
            type="button"
            disabled={submit.busy}
            onClick={() => void regenerate()}
          >
            New recovery codes
          </button>
          <DangerAction
            label="Disable two-factor"
            confirmLabel="Disable it"
            question="Disabling the factor signs out your other devices and leaves this account protected by its password alone."
            onConfirm={() => void disable()}
            busy={submit.busy}
          />
        </div>
      ) : null}
    </Section>
  );
}
