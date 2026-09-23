"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// The identity settings sections, in the order an administrator sets them up:
// policy first (it decides what the rest may do), then the ways in, then the
// machine credentials, then the evidence.
export const IDENTITY_SECTIONS = [
  { href: "/dashboard/settings/identity", label: "Overview" },
  { href: "/dashboard/settings/identity/security", label: "My security" },
  { href: "/dashboard/settings/identity/sso", label: "Single sign-on" },
  { href: "/dashboard/settings/identity/scim", label: "Provisioning" },
  { href: "/dashboard/settings/identity/domains", label: "Domains" },
  { href: "/dashboard/settings/identity/api-keys", label: "API keys" },
  {
    href: "/dashboard/settings/identity/service-accounts",
    label: "Service accounts",
  },
];

export default function IdentityNav() {
  const pathname = usePathname();

  function isActive(href: string): boolean {
    if (href === "/dashboard/settings/identity") {
      return pathname === href;
    }
    return pathname === href || pathname.startsWith(`${href}/`);
  }

  return (
    <nav className="subnav" aria-label="Identity settings">
      {IDENTITY_SECTIONS.map((section) => (
        <Link
          key={section.href}
          href={section.href}
          className={isActive(section.href) ? "active" : undefined}
        >
          {section.label}
        </Link>
      ))}
    </nav>
  );
}
