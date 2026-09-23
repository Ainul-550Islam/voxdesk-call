"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { identityApi, identityErrorReason } from "@/lib/identity";

/**
 * Load once, keep the result, and expose a manual reload.
 *
 * Every identity page needs the same three states and the same rule for a
 * failed load: show what the server said, offer a retry, and never leave the
 * user staring at a spinner. `reload` is stable, so a mutation handler can
 * call it after a write.
 */
export function useLoader<T>(
  loader: () => Promise<T>,
  options: { enabled?: boolean; deps?: readonly unknown[] } = {},
): {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
} {
  const { enabled = true, deps = [] } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [nonce, setNonce] = useState(0);
  const mounted = useRef(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    loaderRef
      .current()
      .then((value) => {
        if (cancelled || !mounted.current) return;
        setData(value);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled || !mounted.current) return;
        setError(identityErrorReason(err));
      })
      .finally(() => {
        if (!cancelled && mounted.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // `deps` is the caller's own dependency list (a filter, a selection) and is
    // what makes a loader that closes over render state re-run when that state
    // changes. `nonce` is `reload()`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, nonce, ...deps]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { data, error, loading, reload };
}

/**
 * Run a privileged write, and handle the two refusals that have a next step.
 *
 * * **428** means the session is fine but its proof of presence is stale. The
 *   action is *remembered*, not lost: `reauthenticate` supplies the missing
 *   proof and then runs exactly the same action again, so the operator does not
 *   have to find the button they pressed.
 * * Anything else is shown as-is, including the specific `reason` the server
 *   attached (for example `emergency_disabled`), because that is the sentence
 *   that tells somebody what to change.
 */
export function useSubmit(): {
  busy: boolean;
  error: string | null;
  notice: string | null;
  needsReauth: boolean;
  run: (action: () => Promise<void>, successMessage?: string) => Promise<boolean>;
  reauthenticate: (credential: { code?: string; password?: string }) => Promise<void>;
  clear: () => void;
} {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [needsReauth, setNeedsReauth] = useState(false);
  const pending = useRef<(() => Promise<void>) | null>(null);

  const execute = useCallback(
    async (
      action: () => Promise<void>,
      successMessage: string,
    ): Promise<boolean> => {
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        await action();
        pending.current = null;
        setNeedsReauth(false);
        if (successMessage) setNotice(successMessage);
        return true;
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 428) {
          pending.current = action;
          setNeedsReauth(true);
        }
        setError(identityErrorReason(err));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const run = useCallback(
    (action: () => Promise<void>, successMessage = "") =>
      execute(action, successMessage),
    [execute],
  );

  const reauthenticate = useCallback(
    async (credential: { code?: string; password?: string }) => {
      setBusy(true);
      setError(null);
      try {
        await identityApi.reauth(credential);
      } catch (err: unknown) {
        setError(identityErrorReason(err));
        setBusy(false);
        return;
      }
      setBusy(false);
      setNeedsReauth(false);
      const action = pending.current;
      if (!action) {
        setNotice("Confirmed. Try the action again.");
        return;
      }
      await execute(action, "Confirmed and applied.");
    },
    [execute],
  );

  const clear = useCallback(() => {
    setError(null);
    setNotice(null);
    setNeedsReauth(false);
  }, []);

  return { busy, error, notice, needsReauth, run, reauthenticate, clear };
}

/** `true` when a refusal is the owner-only rule on an emergency clear. */
export function isOwnerRequired(error: unknown): boolean {
  return error instanceof ApiError && error.code === "owner_required";
}
