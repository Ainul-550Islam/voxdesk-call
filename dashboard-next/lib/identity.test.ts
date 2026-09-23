// Unit tests for the identity client's pure helpers.
//
// These are the parts of the identity UI that can be wrong without a server to
// talk to: a duration rendered in the wrong unit, a slug that disagrees with the
// one the server stores, a status that gets the wrong colour, an error body
// whose specific `reason` is dropped. Everything else is exercised by the API
// itself, so it is not re-tested here.

import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import {
  describeDuration,
  enforcementLabel,
  identityErrorReason,
  missingFields,
  protocolLabel,
  scopeLabel,
  slugify,
  statusTone,
} from "./identity";

describe("describeDuration", () => {
  it("renders minutes in the largest unit that divides them exactly", () => {
    expect(describeDuration(15, "minutes")).toBe("15 minutes");
    expect(describeDuration(60, "minutes")).toBe("1 hour");
    expect(describeDuration(720, "minutes")).toBe("12 hours");
    expect(describeDuration(1440, "minutes")).toBe("1 day");
    expect(describeDuration(2880, "minutes")).toBe("2 days");
  });

  it("uses the singular for one unit and never renders zero", () => {
    expect(describeDuration(1, "minutes")).toBe("1 minute");
    expect(describeDuration(1, "days")).toBe("1 day");
    expect(describeDuration(0, "minutes")).toBe("—");
    expect(describeDuration(Number.NaN, "days")).toBe("—");
  });
});

describe("slugify", () => {
  it("produces what the server will store", () => {
    expect(slugify("Okta")).toBe("okta");
    expect(slugify("Acme — Production IdP")).toBe("acme-production-idp");
    expect(slugify("  spaced  out  ")).toBe("spaced-out");
    expect(slugify("a/b/c")).toBe("a-b-c");
  });

  it("collapses repeats, strips edges and caps the length", () => {
    expect(slugify("Acme---Corp")).toBe("acme-corp");
    expect(slugify("--acme--")).toBe("acme");
    expect(slugify("x".repeat(120)).length).toBe(60);
  });

  it("returns an empty string when nothing usable is left, so the caller can refuse", () => {
    expect(slugify("")).toBe("");
    expect(slugify("!!!")).toBe("");
  });
});

describe("scopeLabel and enforcementLabel", () => {
  it("reads a permission value as a label", () => {
    expect(scopeLabel("scim:users")).toBe("Scim Users");
    expect(scopeLabel("api_key:manage")).toBe("Api Key Manage");
    expect(scopeLabel("call:read")).toBe("Call Read");
    expect(scopeLabel("")).toBe("");
  });

  it("reads an enforcement value as a label, defaulting to Off", () => {
    expect(enforcementLabel("require_sso")).toBe("Require Sso");
    expect(enforcementLabel("off")).toBe("Off");
    expect(enforcementLabel("")).toBe("Off");
  });

  it("labels a protocol only when it is one of the two", () => {
    expect(protocolLabel("oidc")).toBe("OIDC");
    expect(protocolLabel("saml")).toBe("SAML");
    expect(protocolLabel("")).toBe("—");
  });
});

describe("statusTone", () => {
  it("maps good states to the success tone and bad ones to the failure tone", () => {
    for (const value of ["active", "verified", "succeeded", "ok", "enabled"]) {
      expect(statusTone(value)).toBe("completed");
    }
    for (const value of ["disabled", "failed", "revoked", "expired"]) {
      expect(statusTone(value)).toBe("failed");
    }
  });

  it("treats anything it does not know as a caution rather than as success", () => {
    expect(statusTone("pending")).toBe("no_answer");
    expect(statusTone("")).toBe("no_answer");
  });
});

describe("missingFields", () => {
  it("names the empty fields and ignores whitespace", () => {
    expect(
      missingFields({ name: "Okta", issuer: "  " }, ["name", "issuer", "client"]),
    ).toEqual(["issuer", "client"]);
  });

  it("is empty when everything required is present", () => {
    expect(missingFields({ name: "Okta" }, ["name"])).toEqual([]);
  });
});

describe("identityErrorReason", () => {
  it("prefers the server's own message, which carries the specific rule", () => {
    // `PolicyDenied` answers 403 with `code=policy_denied` and the rule in
    // `reason`; the message is what a person should read.
    const error = new ApiError(
      403,
      "This account was disabled by the platform operator.",
      "policy_denied",
      "emergency_disabled",
    );
    expect(identityErrorReason(error)).toBe(
      "This account was disabled by the platform operator.",
    );
    expect(error.reason).toBe("emergency_disabled");
  });

  it("falls back to the error's own message, then to a generic one", () => {
    expect(identityErrorReason(new Error("boom"))).toBe("boom");
    expect(identityErrorReason(undefined)).toBe("request failed");
    expect(identityErrorReason("nope")).toBe("request failed");
  });
});
