"use client";

import { useState, type ReactNode } from "react";

// Destructive and privileged actions.
//
// Every panel that can revoke a credential, disable an identity or lift a
// platform stop uses these, so the shape of the confirmation is the same
// everywhere: a plain sentence naming what will happen, an optional reason the
// server actually requires, and a button that says what it does. No dialog
// library, because a modal that traps focus incorrectly is worse for a screen
// reader than an inline panel that does not move the user at all.

export function DangerAction({
  label,
  confirmLabel,
  question,
  onConfirm,
  reasonLabel,
  reasonPlaceholder,
  reasonMinLength = 0,
  requireReason = false,
  disabled = false,
  busy = false,
}: {
  label: string;
  confirmLabel: string;
  question: string;
  onConfirm: (reason: string) => void | Promise<void>;
  reasonLabel?: string;
  reasonPlaceholder?: string;
  reasonMinLength?: number;
  requireReason?: boolean;
  disabled?: boolean;
  busy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");

  const reasonTooShort =
    reason.trim().length < Math.max(reasonMinLength, requireReason ? 1 : 0);

  if (!open) {
    return (
      <button
        type="button"
        className="danger"
        disabled={disabled || busy}
        onClick={() => setOpen(true)}
      >
        {busy ? "Working…" : label}
      </button>
    );
  }

  return (
    <div className="confirm" role="group" aria-label={label}>
      <p className="confirm-q">{question}</p>
      {reasonLabel ? (
        <div className="field">
          <label htmlFor={`reason-${label}`}>{reasonLabel}</label>
          <input
            id={`reason-${label}`}
            type="text"
            value={reason}
            maxLength={200}
            placeholder={reasonPlaceholder}
            onChange={(event) => setReason(event.target.value)}
          />
          {reasonMinLength > 0 ? (
            <p className="hint">
              At least {reasonMinLength} characters. It is stored with the
              event.
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="confirm-actions">
        <button
          type="button"
          className="danger"
          disabled={busy || (reasonLabel ? reasonTooShort : false)}
          onClick={() => void onConfirm(reason.trim())}
        >
          {busy ? "Working…" : confirmLabel}
        </button>
        <button
          type="button"
          onClick={() => {
            setOpen(false);
            setReason("");
          }}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * A secret the server will never send again.
 *
 * Rendered by every create/rotate flow. The value is selectable text in a
 * monospace block, marked `readOnly` so a stray keystroke cannot corrupt it,
 * and the panel stays until the operator dismisses it — closing it early is the
 * only way to lose the value.
 */
export function SecretOnce({
  label,
  secret,
  warning,
  onDismiss,
  extra,
}: {
  label: string;
  secret: string;
  warning: string;
  onDismiss: () => void;
  extra?: ReactNode;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
    } catch {
      // Clipboard access can be denied; the value is on screen either way.
      setCopied(false);
    }
  }

  return (
    <div className="secret" role="alert">
      <strong>{label}</strong>
      <code className="secret-value">{secret}</code>
      <p className="hint">{warning}</p>
      {extra}
      <div className="confirm-actions">
        <button type="button" className="primary" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" onClick={onDismiss}>
          I have stored it
        </button>
      </div>
    </div>
  );
}

/**
 * Shown when the server answers 428.
 *
 * A fresh proof of presence is required — a privileged action on a session that
 * has been open a while. The panel says which proof the account can give, and
 * the caller passes the same values to `POST /api/identity/reauth`.
 */
export function ReauthPrompt({
  onReauthenticate,
  canUseCode,
  canUsePassword,
  busy = false,
}: {
  onReauthenticate: (credential: {
    code?: string;
    password?: string;
  }) => void | Promise<void>;
  canUseCode: boolean;
  canUsePassword: boolean;
  busy?: boolean;
}) {
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const useCode = canUseCode;

  return (
    <div className="reauth" role="alert">
      <strong>Confirm it is you</strong>
      <p className="hint">
        This change needs a fresh sign-in. Nothing was saved yet.
        {useCode
          ? " Enter a code from your authenticator app (or a recovery code)."
          : " Enter your password."}
      </p>
      {useCode ? (
        <div className="field">
          <label htmlFor="reauth-code">Authenticator code</label>
          <input
            id="reauth-code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
          />
        </div>
      ) : (
        <div className="field">
          <label htmlFor="reauth-password">Password</label>
          <input
            id="reauth-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
      )}
      <div className="confirm-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || (useCode ? code.trim().length < 6 : password.length < 1)}
          onClick={() =>
            void onReauthenticate(useCode ? { code: code.trim() } : { password })
          }
        >
          {busy ? "Confirming…" : "Confirm"}
        </button>
        {canUseCode && canUsePassword ? (
          <button type="button" onClick={() => onReauthenticate({ password })}>
            Use password instead
          </button>
        ) : null}
      </div>
    </div>
  );
}

/** A one-line result banner. `tone` decides the styling, never the wording. */
export function ResultBanner({
  error,
  notice,
}: {
  error?: string | null;
  notice?: string | null;
}) {
  if (error) {
    return (
      <p className="error-banner" role="alert">
        {error}
      </p>
    );
  }
  if (notice) {
    return (
      <p className="notice" role="status">
        {notice}
      </p>
    );
  }
  return null;
}
