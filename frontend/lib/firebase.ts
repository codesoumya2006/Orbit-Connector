import {
  getApp,
  getApps,
  initializeApp,
  type FirebaseApp,
} from "firebase/app";
import {
  browserLocalPersistence,
  getAuth,
  GoogleAuthProvider,
  setPersistence,
  signInWithPopup,
  signOut,
  type Auth,
  type User,
} from "firebase/auth";

type FirebaseClientConfig = {
  apiKey: string;
  authDomain: string;
  projectId: string;
  appId: string;
};

let firebaseApp: FirebaseApp | undefined;
let firebaseAuth: Auth | undefined;
let initialization: Promise<Auth> | undefined;

export const auth: Pick<Auth, "currentUser"> = {
  get currentUser() {
    return firebaseAuth?.currentUser ?? null;
  },
};

function getClientConfig(): FirebaseClientConfig {
  const config = {
    apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
    authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
    projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
    appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID,
  };
  const missing = Object.entries(config)
    .filter(([, value]) => !value?.trim())
    .map(([key]) => `NEXT_PUBLIC_FIREBASE_${key.replace(/[A-Z]/g, (letter) => `_${letter}`).toUpperCase()}`);

  if (missing.length) {
    throw new Error(`Missing Firebase client configuration: ${missing.join(", ")}`);
  }

  return config as FirebaseClientConfig;
}

export function initializeFirebaseAuth(): Promise<Auth> {
  if (typeof window === "undefined") {
    return Promise.reject(
      new Error("Firebase Authentication can only be initialized in the browser."),
    );
  }
  if (initialization) {
    return initialization;
  }

  try {
    const config = getClientConfig();
    firebaseApp = getApps().length ? getApp() : initializeApp(config);
    firebaseAuth = getAuth(firebaseApp);
    const authInstance = firebaseAuth;
    initialization = setPersistence(authInstance, browserLocalPersistence)
      .then(() => authInstance)
      .catch((error: unknown) => {
        initialization = undefined;
        firebaseAuth = undefined;
        throw error;
      });
    return initialization;
  } catch (error) {
    initialization = undefined;
    return Promise.reject(error);
  }
}

export function normalizeAuthError(error: unknown): Error {
  if (error instanceof Error) {
    const code = (error as { code?: string }).code;
    switch (code) {
      case "auth/popup-blocked":
        return new Error(
          "The sign-in popup was blocked by your browser. Please allow popups for localhost to sign in.",
        );
      case "auth/popup-closed-by-user":
        return new Error("Sign-in popup was closed before completing. Please try again.");
      case "auth/cancelled-popup-request":
        return new Error("A sign-in request is already in progress.");
      case "auth/unauthorized-domain":
        return new Error(
          "This domain (localhost) is not authorized in Firebase Console. Add 'localhost' to Authorized Domains in Firebase Authentication Settings.",
        );
      case "auth/network-request-failed":
        return new Error(
          "A network error occurred while connecting to Firebase. Please check your internet connection.",
        );
      case "auth/operation-not-allowed":
        return new Error(
          "Google sign-in is not enabled in Firebase Console. Please enable Google in Firebase Authentication > Sign-in method.",
        );
      case "auth/account-exists-with-different-credential":
        return new Error(
          "An account already exists with the same email using a different sign-in method.",
        );
      default:
        return error;
    }
  }
  return new Error("Authentication failed.");
}

export async function signInWithGoogle(): Promise<User> {
  const initializedAuth = await initializeFirebaseAuth();
  try {
    const result = await signInWithPopup(
      initializedAuth,
      new GoogleAuthProvider(),
    );
    return result.user;
  } catch (error: unknown) {
    throw normalizeAuthError(error);
  }
}

export async function signOutFirebase(): Promise<void> {
  const initializedAuth = await initializeFirebaseAuth();
  await signOut(initializedAuth);
}
