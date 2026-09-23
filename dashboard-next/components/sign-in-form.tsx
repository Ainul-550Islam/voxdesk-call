"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { getToken, setToken } from "@/lib/auth";
import { identityApi } from "@/lib/identity";
import type { MFAChallengeResponse } from "@/lib/types";

/**
 * Sign-in, including the second-factor step.
 *
 * The server answers `/auth/login` with **202** and a challenge when the
 * account owes a factor, and with tokens otherwise. Both are handled here: the
 * form swaps to a code prompt instead of treating a 202 as a completed sign-in,
 * which is the mistake a client that only checks "is this 2xx?" makes.
 */
export default function SignInForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<MFAChallengeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (getToken()) router.replace("/dashboard");
  }, [router]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api.login(email.trim(), password);
      if (result.kind === "mfa_challenge") {
        // Not a failure and not a success: no tokens exist yet, so the only
        // way forward is the code.
        setChallenge(result.challenge);
        setCode("");
        return;
      }
      setToken(result.tokens.access_token);
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in");
    } finally {
      setBusy(false);
    }
  }

  async function onVerify(event: FormEvent) {
    event.preventDefault();
    if (!challenge) return;
    setBusy(true);
    setError(null);
    try {
      const tokens = await identityApi.loginMfaVerify(
        challenge.challenge,
        code.trim(),
      );
      setToken(tokens.access_token);
      router.replace("/dashboard");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "That code could not be verified",
      );
    } finally {
      setBusy(false);
    }
  }

  if (challenge) {
    return (
      <form className="card auth-card" onSubmit={onVerify}>
        <h1>VoxDesk</h1>
        <p className="muted">
          Two-factor authentication is required for this account.
        </p>

        <label htmlFor="mfa-code">Authenticator code</label>
        <input
          id="mfa-code"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          autoFocus
          required
          value={code}
          onChange={(event) => setCode(event.target.value)}
          placeholder="123456"
        />
        <p className="hint">
          Enter the six-digit code from your authenticator app, or one of your
          recovery codes. The request expires in {challenge.expires_in}{" "}
          seconds.
        </p>

        {error ? (
          <p className="error" role="alert">
            {error}
          </p>
        ) : null}

        <button type="submit" disabled={busy || code.trim().length < 6}>
          {busy ? "Verifying…" : "Verify"}
        </button>
        <button
          type="button"
          onClick={() => {
            setChallenge(null);
            setCode("");
            setError(null);
          }}
        >
          Start over
        </button>
      </form>
    );
  }

  return (
    <form className="card auth-card" onSubmit={onSubmit}>
      <h1>VoxDesk</h1>
      <p className="muted">Sign in to your workspace</p>

      <label htmlFor="email">Email</label>
      <input
        id="email"
        type="email"
        autoComplete="email"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="you@company.com"
      />

      <label htmlFor="password">Password</label>
      <input
        id="password"
        type="password"
        autoComplete="current-password"
        required
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="••••••••"
      />

      {error ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}

      <button type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
