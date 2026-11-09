"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { onAuthStateChanged, type User } from "firebase/auth";
import { apiFetch } from "@/lib/api";
import {
  auth,
  initializeFirebaseAuth,
  signInWithGoogle,
  signOutFirebase,
} from "@/lib/firebase";
import { isDashboardPreviewMode } from "@/lib/dashboard-preview";

type AuthContextValue = {
  user: User | null;
  loading: boolean;
  signingIn: boolean;
  error: string | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  clearError: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Authentication failed.";
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [signingIn, setSigningIn] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let unsubscribe: (() => void) | undefined;

    void initializeFirebaseAuth()
      .then((initializedAuth) => {
        if (!active) return;
        unsubscribe = onAuthStateChanged(
          initializedAuth,
          (nextUser) => {
            if (!active) return;
            setUser(nextUser);
            setLoading(false);
          },
          (authError) => {
            if (!active) return;
            setError(errorMessage(authError));
            setLoading(false);
          },
        );
      })
      .catch((authError: unknown) => {
        if (!active) return;
        setError(errorMessage(authError));
        setLoading(false);
      });

    return () => {
      active = false;
      unsubscribe?.();
    };
  }, []);

  const clearError = useCallback(() => {
    setError(null);
  }, []);

  const signIn = useCallback(async () => {
    if (signingIn) return;
    setError(null);
    setSigningIn(true);
    try {
      await signInWithGoogle();
    } catch (signInError) {
      setError(errorMessage(signInError));
    } finally {
      setSigningIn(false);
    }
  }, [signingIn]);

  const signOut = useCallback(async () => {
    setError(null);
    if (!isDashboardPreviewMode()) {
      void apiFetch(`${API}/auth/logout`, { method: "POST" }).catch(
        (logoutError: unknown) => {
          console.warn("Logout audit request failed.", logoutError);
        },
      );
    }
    try {
      await signOutFirebase();
      setUser(null);
    } catch (signOutError) {
      setError(errorMessage(signOutError));
    }
  }, []);

  const value = useMemo(
    () => ({ user, loading, signingIn, error, signIn, signOut, clearError }),
    [user, loading, signingIn, error, signIn, signOut, clearError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used within AuthProvider.");
  }
  return value;
}
