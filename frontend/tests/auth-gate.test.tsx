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

vi.mock("@/components/Dashboard", () => ({
  default: () => <p>Connector dashboard</p>,
}));

import AuthGate from "@/components/AuthGate";

const user = {
  uid: "firebase-uid",
  email: "user@example.com",
  emailVerified: true,
};

describe("AuthGate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.useAuth.mockReturnValue({
      user: null,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mocks.apiFetch.mockResolvedValue(
      new Response(JSON.stringify([]), { status: 200 }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("offers Google sign-in while signed out", () => {
    const signIn = vi.fn();
    mocks.useAuth.mockReturnValue({
      user: null,
      loading: false,
      signingIn: false,
      error: null,
      signIn,
      signOut: vi.fn(),
    });

    render(<AuthGate />);

    expect(screen.getByText("Welcome to Orbit")).toBeTruthy();
    expect(screen.queryByText("Connector dashboard")).toBeNull();

    const button = screen.getByRole("button", {
      name: /continue with google|sign in with google/i,
    });
    expect(button).toBeTruthy();
    fireEvent.click(button);
    expect(signIn).toHaveBeenCalledOnce();
  });

  it("shows loading state and disables the button while authenticating", () => {
    mocks.useAuth.mockReturnValue({
      user: null,
      loading: false,
      signingIn: true,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });

    render(<AuthGate />);

    expect(screen.getByText(/connecting to google/i)).toBeTruthy();
    const button = screen.getByRole("button", {
      name: /continue with google|sign in with google/i,
    });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("displays an authentication error alert when sign-in fails", () => {
    mocks.useAuth.mockReturnValue({
      user: null,
      loading: false,
      signingIn: false,
      error: "The sign-in popup was blocked by your browser. Please allow popups for localhost to sign in.",
      signIn: vi.fn(),
      signOut: vi.fn(),
    });

    render(<AuthGate />);

    expect(
      screen.getByText(/The sign-in popup was blocked by your browser/i),
    ).toBeTruthy();
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("shows session checking screen during initial auth verification", () => {
    mocks.useAuth.mockReturnValue({
      user: null,
      loading: true,
      signingIn: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });

    render(<AuthGate />);

    expect(screen.getByText("Checking your session…")).toBeTruthy();
    expect(screen.queryByText("Connector dashboard")).toBeNull();
  });

  it("keeps unauthenticated users on the login screen in preview mode", () => {
    vi.stubEnv("NEXT_PUBLIC_DASHBOARD_PREVIEW_MODE", "true");

    render(<AuthGate />);

    expect(screen.getByText("Welcome to Orbit")).toBeTruthy();
    expect(screen.getByRole("button", { name: /continue with google/i })).toBeTruthy();
    expect(screen.queryByText("Connector dashboard")).toBeNull();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("opens the dashboard without requesting organizations in preview mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_DASHBOARD_PREVIEW_MODE", "true");
    mocks.useAuth.mockReturnValue({
      user,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mocks.apiFetch.mockResolvedValue(
      new Response("Service unavailable", { status: 503 }),
    );

    render(<AuthGate />);

    expect(await screen.findByText("Connector dashboard")).toBeTruthy();
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  it("requires a choice when the user has multiple organizations", async () => {
    mocks.useAuth.mockReturnValue({
      user,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mocks.apiFetch.mockResolvedValue(
      new Response(
        JSON.stringify([
          { id: "org-one", name: "One", role: "owner" },
          { id: "org-two", name: "Two", role: "viewer" },
        ]),
        { status: 200 },
      ),
    );

    render(<AuthGate />);

    expect(
      await screen.findByRole("combobox", { name: /organization/i }),
    ).toBeTruthy();
    expect(screen.queryByText("Connector dashboard")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: /organization/i }), {
      target: { value: "org-two" },
    });

    expect(await screen.findByText("Connector dashboard")).toBeTruthy();
  });

  it("creates an organization for an authenticated user with no memberships", async () => {
    mocks.useAuth.mockReturnValue({
      user,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mocks.apiFetch
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ id: "new-org", name: "New workspace", role: "owner" }),
          { status: 201 },
        ),
      );

    render(<AuthGate />);

    fireEvent.change(await screen.findByLabelText(/organization name/i), {
      target: { value: "New workspace" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create organization/i }));

    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/organizations"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText("Connector dashboard")).toBeTruthy();
  });

  it("accepts an invitation without relying on client-provided user identity", async () => {
    mocks.useAuth.mockReturnValue({
      user,
      loading: false,
      error: null,
      signIn: vi.fn(),
      signOut: vi.fn(),
    });
    mocks.apiFetch
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ id: "invited-org", name: "Invited", role: "viewer" }),
          { status: 201 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify([{ id: "invited-org", name: "Invited", role: "viewer" }]),
          { status: 200 },
        ),
      );

    render(<AuthGate />);

    fireEvent.change(await screen.findByLabelText(/invitation token/i), {
      target: { value: "one-time-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: /accept invitation/i }));

    await waitFor(() =>
      expect(mocks.apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/invitations/accept"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ token: "one-time-token" }),
        }),
      ),
    );
    expect(await screen.findByText("Connector dashboard")).toBeTruthy();
  });
});
