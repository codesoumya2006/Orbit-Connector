"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/contexts/AuthProvider";
import Dashboard from "@/components/Dashboard";
import {
  isDashboardPreviewMode,
  PREVIEW_WORKSPACE,
} from "@/lib/dashboard-preview";

type OrganizationSummary = {
  id: string;
  name: string;
  role: "owner" | "admin" | "viewer";
};

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

function GoogleLogo({ size = 18 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0 }}
    >
      <path
        fill="#4285F4"
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
      />
      <path
        fill="#34A853"
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
      />
      <path
        fill="#FBBC05"
        d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"
      />
      <path
        fill="#EA4335"
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"
      />
    </svg>
  );
}

export default function AuthGate() {
  const {
    user,
    loading: authLoading,
    signingIn,
    error: authError,
    signIn,
    signOut,
  } = useAuth();
  const [organizations, setOrganizations] = useState<OrganizationSummary[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [organizationsLoading, setOrganizationsLoading] = useState(false);
  const [organizationName, setOrganizationName] = useState("");
  const [invitationToken, setInvitationToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const previewMode = isDashboardPreviewMode();

  useEffect(() => {
    let active = true;
    setOrganizations([]);
    setOrganizationId("");
    setError(null);
    if (!user || previewMode) {
      setOrganizationsLoading(false);
      return () => {
        active = false;
      };
    }

    setOrganizationsLoading(true);
    void apiFetch(`${API}/organizations`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not load your organizations.");
        return (await response.json()) as OrganizationSummary[];
      })
      .then((items) => {
        if (!active) return;
        setOrganizations(items);
        if (items.length === 1) setOrganizationId(items[0].id);
      })
      .catch((loadError: unknown) => {
        if (active) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Could not load your organizations.",
          );
        }
      })
      .finally(() => {
        if (active) setOrganizationsLoading(false);
      });

    return () => {
      active = false;
    };
  }, [user?.uid, previewMode]);

  async function createOrganization(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await apiFetch(`${API}/organizations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: organizationName }),
      });
      if (!response.ok) throw new Error("Could not create the organization.");
      const organization = (await response.json()) as OrganizationSummary;
      setOrganizations((current) => [...current, organization]);
      setOrganizationId(organization.id);
    } catch (createError) {
      setError(
        createError instanceof Error
          ? createError.message
          : "Could not create the organization.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function acceptInvitation(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const response = await apiFetch(`${API}/invitations/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: invitationToken }),
      });
      if (!response.ok) throw new Error("Could not accept this invitation.");

      const organizationsResponse = await apiFetch(`${API}/organizations`);
      if (!organizationsResponse.ok) {
        throw new Error("Invitation accepted, but organizations could not be loaded.");
      }
      const items =
        (await organizationsResponse.json()) as OrganizationSummary[];
      setOrganizations(items);
      if (items.length === 1) setOrganizationId(items[0].id);
    } catch (acceptError) {
      setError(
        acceptError instanceof Error
          ? acceptError.message
          : "Could not accept this invitation.",
      );
    } finally {
      setSaving(false);
    }
  }

  if (authLoading) {
    return (
      <main className="auth-screen">
        <div className="auth-card auth-loading-state">
          <div className="brand">
            <div className="brandmark">O</div>
            <span>orbit<span className="branddot">.</span></span>
          </div>
          <div className="auth-spinner-row">
            <div className="spinner" />
            <p>Checking your session…</p>
          </div>
        </div>
      </main>
    );
  }
  if (!user) {
    return (
      <main className="auth-screen">
        <section className="auth-card">
          <div className="brand">
            <div className="brandmark">O</div>
            <span>orbit<span className="branddot">.</span></span>
          </div>
          <div className="auth-header">
            <h1>Welcome to Orbit</h1>
            <p>Connect, organize and sync your business data sources.</p>
          </div>
          {authError && (
            <div className="auth-error" role="alert">
              <span className="auth-error-icon">!</span>
              <div>
                <strong>Authentication Notice</strong>
                <p>{authError}</p>
              </div>
            </div>
          )}
          <div className="auth-actions">
            <button
              className="google-signin-btn"
              onClick={() => void signIn()}
              disabled={signingIn}
              aria-label="Continue with Google"
              type="button"
            >
              {signingIn ? (
                <>
                  <div className="spinner-small" />
                  <span>Connecting to Google…</span>
                </>
              ) : (
                <>
                  <GoogleLogo size={18} />
                  <span>Continue with Google</span>
                </>
              )}
            </button>
          </div>
          <div className="auth-trust-note">
            <span className="trust-dot" />
            <span>Secure authentication via Firebase · Credentials encrypted at rest</span>
          </div>
        </section>
      </main>
    );
  }
  if (previewMode) {
    return (
      <Dashboard
        previewMode
        previewWorkspace={PREVIEW_WORKSPACE}
      />
    );
  }
  if (organizationsLoading) {
    return <main className="auth-screen"><p>Loading your organizations…</p></main>;
  }
  if (error) {
    return (
      <main className="auth-screen">
        <section className="auth-card">
          <p className="auth-error" role="alert">{error}</p>
          <button className="secondary" onClick={() => void signOut()}>Sign out</button>
        </section>
      </main>
    );
  }
  if (organizations.length === 0) {
    return (
      <main className="auth-screen">
        <section className="auth-card">
          <h1>Set up your workspace</h1>
          <p>Create an organization or accept an invitation.</p>
          <form onSubmit={createOrganization}>
            <label htmlFor="organization-name">Organization name</label>
            <input
              id="organization-name"
              required
              minLength={2}
              maxLength={160}
              value={organizationName}
              onChange={(event) => setOrganizationName(event.target.value)}
            />
            <button className="primary" disabled={saving} type="submit">
              Create organization
            </button>
          </form>
          <form onSubmit={acceptInvitation}>
            <label htmlFor="invitation-token">Invitation token</label>
            <input
              id="invitation-token"
              required
              value={invitationToken}
              onChange={(event) => setInvitationToken(event.target.value)}
            />
            <button className="secondary" disabled={saving} type="submit">
              Accept invitation
            </button>
          </form>
          <button className="textbtn" onClick={() => void signOut()}>Sign out</button>
        </section>
      </main>
    );
  }
  if (!organizationId) {
    return (
      <main className="auth-screen">
        <section className="auth-card">
          <label htmlFor="organization-picker">Choose an organization</label>
          <select
            id="organization-picker"
            aria-label="Organization"
            value={organizationId}
            onChange={(event) => setOrganizationId(event.target.value)}
          >
            <option value="">Select an organization</option>
            {organizations.map((organization) => (
              <option key={organization.id} value={organization.id}>
                {organization.name} ({organization.role})
              </option>
            ))}
          </select>
          <button className="textbtn" onClick={() => void signOut()}>Sign out</button>
        </section>
      </main>
    );
  }

  return (
    <Dashboard
      organizationId={organizationId}
      organizations={organizations}
      onOrganizationChange={setOrganizationId}
    />
  );
}
