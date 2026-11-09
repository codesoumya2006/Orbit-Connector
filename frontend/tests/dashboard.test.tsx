// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  useAuth: vi.fn(),
  apiFetch: vi.fn(),
}));

vi.mock("@/contexts/AuthProvider", () => ({
  useAuth: mocks.useAuth,
}));

vi.mock("@/lib/api", () => ({
  apiFetch: mocks.apiFetch,
}));

import Dashboard from "@/components/Dashboard";

const user = {
  uid: "firebase-uid-test",
  email: "developer@example.com",
  displayName: "Dev User",
  emailVerified: true,
};

describe("Dashboard component", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.useAuth.mockReturnValue({
      user,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/providers")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                key: "google_analytics",
                name: "Google Analytics 4",
                category: "Analytics",
                description: "GA4 integration",
                auth_type: "OAuth 2.0",
                setup_state: "ready",
                connection_count: 1,
              },
            ]),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/connections")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "conn-1",
                provider_key: "google_analytics",
                display_name: "Google Analytics 4",
                status: "connected",
                connection_type: "oauth",
              },
            ]),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/custom-connectors")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "custom-1",
                name: "My Custom API",
                base_url: "https://api.custom.com",
                endpoint_path: "/data",
                auth_type: "bearer",
                has_secret: true,
              },
            ]),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("renders with user display name and workspace info", async () => {
    render(
      <Dashboard
        organizationId="org-uuid-1"
        organizations={[
          { id: "org-uuid-1", name: "Acme Corp", role: "owner" },
        ]}
      />,
    );

    expect(await screen.findByText("Good morning, Dev User.")).toBeTruthy();
    expect(screen.getByText("Acme Corp")).toBeTruthy();
    expect(screen.getByText("Owner role")).toBeTruthy();
    expect(screen.getByText("developer@example.com")).toBeTruthy();
  });

  it("renders clearly labeled demo metrics without loading backend data", async () => {
    render(
      <Dashboard
        previewMode
        previewWorkspace={{ name: "Orbit Preview Workspace" }}
      />,
    );

    expect(await screen.findByText(/illustrative demo data/i)).toBeTruthy();
    expect(screen.getByText("Orbit Preview Workspace")).toBeTruthy();
    expect(screen.getByText("18,420")).toBeTruthy();
    expect(screen.getByText("Google Analytics 4")).toBeTruthy();
    expect(screen.getByText("Demo")).toBeTruthy();
    expect(screen.queryByText("Connected")).toBeNull();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("marks backend connector actions unavailable in preview mode", async () => {
    render(
      <Dashboard
        previewMode
        previewWorkspace={{ name: "Orbit Preview Workspace" }}
      />,
    );

    const addButton = await screen.findByRole("button", { name: /add a connector/i });
    expect((addButton as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(addButton);
    expect(screen.getByRole("status").textContent).toMatch(
      /unavailable in preview mode/i,
    );
    const connectButtons = screen.getAllByRole("button", { name: /^connect/i });
    expect(connectButtons.every((button) => (button as HTMLButtonElement).disabled))
      .toBe(true);
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("fetches connections and custom connectors scoped to organizationId using apiFetch", async () => {
    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "admin" },
        ]}
      />,
    );

    await waitFor(() => {
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("connections?organization_id=org-uuid-123"),
      );
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("custom-connectors?organization_id=org-uuid-123"),
      );
    });

    expect(await screen.findByText("My Custom API")).toBeTruthy();
  });

  it("calls signOut when sign out icon button is clicked", async () => {
    const signOut = vi.fn();
    mocks.useAuth.mockReturnValue({
      user,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut,
    });

    render(
      <Dashboard
        organizationId="org-uuid-1"
        organizations={[
          { id: "org-uuid-1", name: "Acme Corp", role: "owner" },
        ]}
      />,
    );

    const signOutBtn = await screen.findByRole("button", { name: /sign out/i });
    fireEvent.click(signOutBtn);
    expect(signOut).toHaveBeenCalledOnce();
  });

  it("displays secret presence indicator for custom connectors", async () => {
    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "admin" },
        ]}
      />,
    );

    expect(await screen.findByText("Stored credential")).toBeTruthy();
  });

  it("disables mutation buttons for viewer role", async () => {
    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "viewer" },
        ]}
      />,
    );

    await screen.findByText("My Custom API");
    const addBtn = screen.getByRole("button", { name: /add a connector/i });
    expect((addBtn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByTitle("Delete custom connector")).toBeNull();
  });

  it("handles Google Analytics OAuth flow initiation", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/providers")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                key: "google_analytics",
                name: "Google Analytics 4",
                category: "Analytics",
                description: "GA4 integration",
                auth_type: "OAuth 2.0",
                setup_state: "ready",
                connection_count: 0,
              },
            ]),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/oauth/google/authorize")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=xyz",
              state: "xyz",
              connection_id: "conn-ga-new",
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "admin" },
        ]}
      />,
    );

    const connectBtn = await screen.findByRole("button", { name: /^connect/i });
    fireEvent.click(connectBtn);

    await waitFor(() => {
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/oauth/google/authorize"),
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ "X-Organization-ID": "org-uuid-123" }),
        }),
      );
    });
  });

  it("displays Sync and Reports buttons for connected GA4 integration", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/connections")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "conn-ga-1",
                provider_key: "google_analytics",
                display_name: "Google Analytics 4",
                status: "connected",
                connection_type: "oauth",
                property_id: "properties/12345",
                property_name: "My Site GA4",
              },
            ]),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "owner" },
        ]}
      />,
    );

    expect(await screen.findByRole("button", { name: /sync/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /reports/i })).toBeTruthy();
    });

  it("handles Google Ads OAuth flow initiation", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/providers")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                key: "google_ads",
                name: "Google Ads",
                category: "Advertising",
                description: "Campaign reporting",
                auth_type: "OAuth 2.0",
                setup_state: "ready",
                connection_count: 0,
              },
            ]),
            { status: 200 },
          ),
        );
      }
      if (url.includes("/oauth/google-ads/authorize")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=ads-xyz",
              state: "ads-xyz",
              connection_id: "conn-ads-new",
            }),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "admin" },
        ]}
      />,
    );

    const connectBtn = await screen.findByRole("button", { name: /^connect/i });
    fireEvent.click(connectBtn);

    await waitFor(() => {
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/oauth/google-ads/authorize"),
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ "X-Organization-ID": "org-uuid-123" }),
        }),
      );
    });
  });

  it("displays Sync and Reports buttons for connected Google Ads integration", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/connections")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "conn-ads-1",
                provider_key: "google_ads",
                display_name: "Google Ads",
                status: "connected",
                connection_type: "oauth",
                customer_id: "1234567890",
                customer_name: "Orbit Marketing Account",
              },
            ]),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "owner" },
        ]}
      />,
    );

    expect(await screen.findByRole("button", { name: /sync/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /reports/i })).toBeTruthy();
    expect(screen.getByText("Orbit Marketing Account (1234567890)")).toBeTruthy();
  });

  it("displays Select customer button when Google Ads connection needs customer selection", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/connections")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "conn-ads-2",
                provider_key: "google_ads",
                display_name: "Google Ads",
                status: "needs_customer_selection",
                connection_type: "oauth",
              },
            ]),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[
          { id: "org-uuid-123", name: "Test Org", role: "admin" },
        ]}
      />,
    );

    expect(await screen.findByText("Customer required")).toBeTruthy();
    expect(screen.getByRole("button", { name: /select customer/i })).toBeTruthy();
  });

  it("renders Supermetrics connected state with sync, reports, and source label", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/connections")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "conn-sm-1",
                provider_key: "supermetrics",
                display_name: "Supermetrics Enterprise",
                status: "connected",
                connection_type: "api_key",
                has_secret: true,
                ds_id: "AW",
                ds_name: "Google Ads",
                ds_accounts: "111-222-3333",
              },
            ]),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[{ id: "org-uuid-123", name: "Test Org", role: "owner" }]}
      />,
    );

    expect(await screen.findByRole("button", { name: /sync/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /reports/i })).toBeTruthy();
    expect(screen.getByText("Google Ads (AW) · 111-222-3333")).toBeTruthy();
  });

  it("renders Groq connected state with test prompt and config buttons", async () => {
    mocks.apiFetch.mockImplementation((url: string) => {
      if (url.includes("/connections")) {
        return Promise.resolve(
          new Response(
            JSON.stringify([
              {
                id: "conn-groq-1",
                provider_key: "groq",
                display_name: "Groq LPU",
                status: "connected",
                connection_type: "api_key",
                has_secret: true,
                selected_model: "llama-3.3-70b-versatile",
              },
            ]),
            { status: 200 },
          ),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });

    render(
      <Dashboard
        organizationId="org-uuid-123"
        organizations={[{ id: "org-uuid-123", name: "Test Org", role: "owner" }]}
      />,
    );

    expect(await screen.findByRole("button", { name: /test prompt/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /config/i })).toBeTruthy();
    expect(screen.getByText("Model: llama-3.3-70b-versatile")).toBeTruthy();
  });
});
