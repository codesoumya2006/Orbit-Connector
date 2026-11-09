// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { onAuthStateChanged } from "firebase/auth";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const firebaseMocks = vi.hoisted(() => ({
  initializeFirebaseAuth: vi.fn(),
  signInWithGoogle: vi.fn(),
  signOutFirebase: vi.fn(),
  apiFetch: vi.fn(),
  auth: { currentUser: null },
  listener: undefined as
    | ((user: { uid: string; email: string | null } | null) => void)
    | undefined,
}));

vi.mock("@/lib/firebase", () => ({
  auth: firebaseMocks.auth,
  initializeFirebaseAuth: firebaseMocks.initializeFirebaseAuth,
  signInWithGoogle: firebaseMocks.signInWithGoogle,
  signOutFirebase: firebaseMocks.signOutFirebase,
}));

vi.mock("@/lib/api", () => ({
  apiFetch: firebaseMocks.apiFetch,
}));

vi.mock("firebase/auth", () => ({
  onAuthStateChanged: vi.fn((_auth, callback) => {
    firebaseMocks.listener = callback;
    return vi.fn();
  }),
}));

import { AuthProvider, useAuth } from "@/contexts/AuthProvider";

function AuthState() {
  const { user, loading, signIn, signOut } = useAuth();
  if (loading) return <p>Loading authentication</p>;
  return (
    <div>
      <p>{user?.email ?? "Signed out"}</p>
      <button onClick={signIn}>Google sign in</button>
      <button onClick={() => void signOut()}>Sign out</button>
    </div>
  );
}

describe("AuthProvider", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    firebaseMocks.listener = undefined;
    firebaseMocks.initializeFirebaseAuth.mockResolvedValue(firebaseMocks.auth);
    firebaseMocks.signInWithGoogle.mockResolvedValue(undefined);
    firebaseMocks.signOutFirebase.mockResolvedValue(undefined);
    firebaseMocks.apiFetch.mockResolvedValue(new Response(null, { status: 204 }));
  });

  it("waits for Firebase auth state and exposes the signed-in user", async () => {
    render(
      <AuthProvider>
        <AuthState />
      </AuthProvider>,
    );

    expect(screen.getByText("Loading authentication")).toBeTruthy();
    await waitFor(() => expect(onAuthStateChanged).toHaveBeenCalledOnce());
    firebaseMocks.listener?.({
      uid: "firebase-uid",
      email: "user@example.com",
    });

    expect(await screen.findByText("user@example.com")).toBeTruthy();
  });

  it("starts Google sign-in from the auth context", async () => {
    render(
      <AuthProvider>
        <AuthState />
      </AuthProvider>,
    );
    await waitFor(() => expect(onAuthStateChanged).toHaveBeenCalledOnce());
    firebaseMocks.listener?.(null);

    fireEvent.click(await screen.findByRole("button", { name: "Google sign in" }));

    await waitFor(() =>
      expect(firebaseMocks.signInWithGoogle).toHaveBeenCalledOnce(),
    );
  });

  it("signs out locally even when logout audit cannot reach the API", async () => {
    firebaseMocks.apiFetch.mockRejectedValue(new Error("network unavailable"));
    render(
      <AuthProvider>
        <AuthState />
      </AuthProvider>,
    );
    await waitFor(() => expect(onAuthStateChanged).toHaveBeenCalledOnce());
    firebaseMocks.listener?.({
      uid: "firebase-uid",
      email: "user@example.com",
    });

    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    await waitFor(() =>
      expect(firebaseMocks.signOutFirebase).toHaveBeenCalledOnce(),
    );
    expect(firebaseMocks.apiFetch).toHaveBeenCalledOnce();
  });

  it("keeps Firebase sign-out working without a backend audit request in preview mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_DASHBOARD_PREVIEW_MODE", "true");
    render(
      <AuthProvider>
        <AuthState />
      </AuthProvider>,
    );
    await waitFor(() => expect(onAuthStateChanged).toHaveBeenCalledOnce());
    firebaseMocks.listener?.({
      uid: "firebase-uid",
      email: "user@example.com",
    });

    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    await waitFor(() =>
      expect(firebaseMocks.signOutFirebase).toHaveBeenCalledOnce(),
    );
    expect(firebaseMocks.apiFetch).not.toHaveBeenCalled();
  });

  it("sets error state when Google sign-in fails", async () => {
    firebaseMocks.signInWithGoogle.mockRejectedValue(
      new Error("The sign-in popup was blocked by your browser. Please allow popups for localhost to sign in."),
    );

    function AuthErrorComponent() {
      const { signIn, error } = useAuth();
      return (
        <div>
          {error && <p role="alert">{error}</p>}
          <button onClick={signIn}>Trigger Sign In</button>
        </div>
      );
    }

    render(
      <AuthProvider>
        <AuthErrorComponent />
      </AuthProvider>,
    );

    await waitFor(() => expect(onAuthStateChanged).toHaveBeenCalledOnce());
    firebaseMocks.listener?.(null);

    fireEvent.click(await screen.findByRole("button", { name: "Trigger Sign In" }));

    expect(
      await screen.findByText(/The sign-in popup was blocked by your browser/i),
    ).toBeTruthy();
  });
});
