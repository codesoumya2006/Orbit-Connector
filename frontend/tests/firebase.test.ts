import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const firebaseMock = vi.hoisted(() => {
  const app = { name: "test-app" };
  const auth = { name: "test-auth" };
  const browserLocalPersistence = { type: "LOCAL" };
  const googleProvider = { providerId: "google.com" };

  return {
    app,
    auth,
    browserLocalPersistence,
    googleProvider,
    initializeApp: vi.fn(() => app),
    getApps: vi.fn(() => []),
    getApp: vi.fn(() => app),
    getAuth: vi.fn(() => auth),
    setPersistence: vi.fn().mockResolvedValue(undefined),
    GoogleAuthProvider: vi.fn(function GoogleAuthProvider() {
      return googleProvider;
    }),
    signInWithPopup: vi.fn(),
    signOut: vi.fn(),
  };
});

vi.mock("firebase/app", () => ({
  initializeApp: firebaseMock.initializeApp,
  getApps: firebaseMock.getApps,
  getApp: firebaseMock.getApp,
}));

vi.mock("firebase/auth", () => ({
  getAuth: firebaseMock.getAuth,
  setPersistence: firebaseMock.setPersistence,
  browserLocalPersistence: firebaseMock.browserLocalPersistence,
  GoogleAuthProvider: firebaseMock.GoogleAuthProvider,
  signInWithPopup: firebaseMock.signInWithPopup,
  signOut: firebaseMock.signOut,
}));

const firebaseConfig = {
  NEXT_PUBLIC_FIREBASE_API_KEY: "test-api-key",
  NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN: "test-project.firebaseapp.com",
  NEXT_PUBLIC_FIREBASE_PROJECT_ID: "test-project",
  NEXT_PUBLIC_FIREBASE_APP_ID: "test-app-id",
};

describe("initializeFirebaseAuth", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.stubEnv("NEXT_PUBLIC_FIREBASE_API_KEY", firebaseConfig.NEXT_PUBLIC_FIREBASE_API_KEY);
    vi.stubEnv(
      "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
      firebaseConfig.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    );
    vi.stubEnv(
      "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
      firebaseConfig.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    );
    vi.stubEnv("NEXT_PUBLIC_FIREBASE_APP_ID", firebaseConfig.NEXT_PUBLIC_FIREBASE_APP_ID);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("initializes Firebase in the browser with local persistence before returning auth", async () => {
    vi.stubGlobal("window", {});
    let finishPersistence!: () => void;
    firebaseMock.setPersistence.mockReturnValue(
      new Promise<void>((resolve) => {
        finishPersistence = resolve;
      }),
    );

    const { initializeFirebaseAuth } = await import("@/lib/firebase");
    expect(firebaseMock.initializeApp).not.toHaveBeenCalled();

    const authPromise = initializeFirebaseAuth();
    await Promise.resolve();

    expect(firebaseMock.initializeApp).toHaveBeenCalledWith({
      apiKey: firebaseConfig.NEXT_PUBLIC_FIREBASE_API_KEY,
      authDomain: firebaseConfig.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
      projectId: firebaseConfig.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
      appId: firebaseConfig.NEXT_PUBLIC_FIREBASE_APP_ID,
    });
    expect(firebaseMock.getAuth).toHaveBeenCalledWith(firebaseMock.app);
    expect(firebaseMock.setPersistence).toHaveBeenCalledWith(
      firebaseMock.auth,
      firebaseMock.browserLocalPersistence,
    );

    let initializationFinished = false;
    void authPromise.then(() => {
      initializationFinished = true;
    });
    await Promise.resolve();
    expect(initializationFinished).toBe(false);

    finishPersistence();
    await expect(authPromise).resolves.toBe(firebaseMock.auth);
  });

  it("does not initialize Firebase when called outside the browser", async () => {
    vi.stubGlobal("window", undefined);
    const { initializeFirebaseAuth } = await import("@/lib/firebase");

    try {
      await initializeFirebaseAuth();
    } catch {
      // The contract only requires that server-side calls do not initialize Firebase.
    }

    expect(firebaseMock.initializeApp).not.toHaveBeenCalled();
    expect(firebaseMock.getAuth).not.toHaveBeenCalled();
    expect(firebaseMock.setPersistence).not.toHaveBeenCalled();
  });

  it.each([
    "NEXT_PUBLIC_FIREBASE_API_KEY",
    "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
    "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
    "NEXT_PUBLIC_FIREBASE_APP_ID",
  ])("reports an error when %s is missing", async (missingVariable) => {
    vi.stubGlobal("window", {});
    delete process.env[missingVariable];

    const { initializeFirebaseAuth } = await import("@/lib/firebase");

    await expect(
      Promise.resolve().then(() => initializeFirebaseAuth()),
    ).rejects.toThrow(missingVariable);
    expect(firebaseMock.initializeApp).not.toHaveBeenCalled();
  });
});

describe("Firebase Google authentication actions", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.stubGlobal("window", {});
    vi.stubEnv("NEXT_PUBLIC_FIREBASE_API_KEY", firebaseConfig.NEXT_PUBLIC_FIREBASE_API_KEY);
    vi.stubEnv(
      "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN",
      firebaseConfig.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    );
    vi.stubEnv(
      "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
      firebaseConfig.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    );
    vi.stubEnv("NEXT_PUBLIC_FIREBASE_APP_ID", firebaseConfig.NEXT_PUBLIC_FIREBASE_APP_ID);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("initializes auth, signs in with Google popup, and returns the user", async () => {
    const user = { uid: "google-user" };
    firebaseMock.signInWithPopup.mockResolvedValue({ user });
    const { signInWithGoogle } = await import("@/lib/firebase");

    await expect(signInWithGoogle()).resolves.toBe(user);

    expect(firebaseMock.getAuth).toHaveBeenCalledWith(firebaseMock.app);
    expect(firebaseMock.setPersistence).toHaveBeenCalledWith(
      firebaseMock.auth,
      firebaseMock.browserLocalPersistence,
    );
    expect(firebaseMock.GoogleAuthProvider).toHaveBeenCalledOnce();
    expect(firebaseMock.signInWithPopup).toHaveBeenCalledWith(
      firebaseMock.auth,
      firebaseMock.googleProvider,
    );
  });

  it("initializes auth and signs out through Firebase", async () => {
    const { signOutFirebase } = await import("@/lib/firebase");

    await signOutFirebase();

    expect(firebaseMock.getAuth).toHaveBeenCalledWith(firebaseMock.app);
    expect(firebaseMock.setPersistence).toHaveBeenCalledWith(
      firebaseMock.auth,
      firebaseMock.browserLocalPersistence,
    );
    expect(firebaseMock.signOut).toHaveBeenCalledWith(firebaseMock.auth);
  });

  it.each([
    ["auth/popup-blocked", "The sign-in popup was blocked by your browser"],
    ["auth/popup-closed-by-user", "Sign-in popup was closed before completing"],
    ["auth/unauthorized-domain", "This domain (localhost) is not authorized in Firebase Console"],
    ["auth/network-request-failed", "A network error occurred while connecting to Firebase"],
    ["auth/operation-not-allowed", "Google sign-in is not enabled in Firebase Console"],
  ])("normalizes %s error into descriptive user message", async (code, expectedSubstring) => {
    const { normalizeAuthError } = await import("@/lib/firebase");
    const fbError = Object.assign(new Error("Raw message"), { code });
    const normalized = normalizeAuthError(fbError);
    expect(normalized.message).toContain(expectedSubstring);
  });
});
