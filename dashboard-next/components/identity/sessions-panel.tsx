"use client";

import { useState } from "react";
import { DangerAction, ResultBanner } from "@/components/identity/actions";
import { Section, TextField } from "@/components/identity/fields";
import { Dash } from "@/components/identity/badges";
import { useSubmit } from "@/components/identity/use-identity";
import { describeDuration, identityApi } from "@/lib/identity";
import type { SessionList } from "@/lib/identity-types";

/**
 * Devices and sessions.
 *
 * The list is the truth about where an account is signed in, and every action
 * here has an immediate effect on the server: renaming is cosmetic, revoking is
 * not. "Sign out everywhere else" keeps the caller signed in — a control that
 * logged the operator out of the page they are using would be a trap.
 */
export default function SessionsPanel({
  sessions,
  currentSessionId,
  onChanged,
}: {
  sessions: SessionList;
  currentSessionId: string | null;
  onChanged: () => void;
}) {
  const submit = useSubmit();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [label, setLabel] = useState("");

  const limits = sessions.limits;

  async function rename(sessionId: string) {
    await submit.run(async () => {
      await identityApi.renameSession(sessionId, label.trim());
      setRenaming(null);
      setLabel("");
      onChanged();
    }, "Device renamed.");
  }

  async function revoke(sessionId: string) {
    await submit.run(async () => {
      await identityApi.revokeSession(sessionId);
      onChanged();
    }, "That device has been signed out.");
  }

  async function revokeOthers() {
    await submit.run(async () => {
      await identityApi.revokeOtherSessions();
      onChanged();
    }, "Every other device has been signed out.");
  }

  async function revokeAll() {
    await submit.run(async () => {
      await identityApi.revokeAllSessions();
      onChanged();
    }, "All sessions have been signed out, including this one.");
  }

  return (
    <Section
      title="Devices and sessions"
      description={`A session ends after ${describeDuration(limits.idle_minutes, "minutes")} idle, or ${describeDuration(limits.refresh_days, "days")} after sign-in. At most ${limits.max_active} can be open at once.`}
      actions={
        <div className="section-actions">
          <button
            type="button"
            disabled={submit.busy}
            onClick={() => void revokeOthers()}
          >
            Sign out other devices
          </button>
        </div>
      }
    >
      <ResultBanner error={submit.error} notice={submit.notice} />

      {sessions.sessions.length === 0 ? (
        <div className="empty">No active sessions.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Device</th>
                <th>Address</th>
                <th>Signed in</th>
                <th>Last seen</th>
                <th>Factor</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sessions.sessions.map((row) => {
                const isCurrent = row.id === currentSessionId || row.current;
                return (
                  <tr key={row.id}>
                    <td>
                      {renaming === row.id ? (
                        <div className="inline-form">
                          <TextField
                            id={`label-${row.id}`}
                            label="Name this device"
                            value={label}
                            onChange={setLabel}
                            placeholder="Office laptop"
                          />
                          <button
                            type="button"
                            className="primary"
                            disabled={submit.busy || label.trim().length === 0}
                            onClick={() => void rename(row.id)}
                          >
                            Save
                          </button>
                          <button
                            type="button"
                            onClick={() => setRenaming(null)}
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <>
                          {row.device || "Unknown device"}
                          {isCurrent ? (
                            <span className="badge in_progress"> this device</span>
                          ) : null}
                          <div className="muted small">{row.user_agent}</div>
                        </>
                      )}
                    </td>
                    <td>{row.ip_address || <Dash />}</td>
                    <td>{row.created_at}</td>
                    <td>{row.last_seen_at}</td>
                    <td>
                      {row.mfa_verified ? (
                        <span className="badge completed">verified</span>
                      ) : (
                        <span className="badge none">password</span>
                      )}
                      <div className="muted small">{row.auth_method}</div>
                    </td>
                    <td className="row-actions">
                      {!isCurrent ? (
                        <>
                          <button
                            type="button"
                            onClick={() => {
                              setRenaming(row.id);
                              setLabel(row.device);
                            }}
                          >
                            Rename
                          </button>
                          <button
                            type="button"
                            className="danger"
                            disabled={submit.busy}
                            onClick={() => void revoke(row.id)}
                          >
                            Sign out
                          </button>
                        </>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="action-row">
        <DangerAction
          label="Sign out everywhere"
          confirmLabel="Sign out all devices"
          question="Every session is ended, including this one. You will need to sign in again."
          onConfirm={() => void revokeAll()}
          busy={submit.busy}
        />
      </div>
    </Section>
  );
}
