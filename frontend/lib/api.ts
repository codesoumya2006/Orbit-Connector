import { auth } from "@/lib/firebase";

export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const user = auth.currentUser;
  if (!user) {
    return fetch(input, init);
  }

  const token = await user.getIdToken();
  if (!token) {
    return fetch(input, init);
  }

  const headers = new Headers(
    typeof Request !== "undefined" && input instanceof Request
      ? input.headers
      : undefined,
  );
  new Headers(init?.headers).forEach((value, name) => {
    headers.set(name, value);
  });
  headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(input, { ...init, headers });
  if (response.status !== 401) {
    return response;
  }

  const refreshedToken = await user.getIdToken(true);
  if (!refreshedToken) {
    return response;
  }

  const refreshedHeaders = new Headers(headers);
  refreshedHeaders.set("Authorization", `Bearer ${refreshedToken}`);
  return fetch(input, { ...init, headers: refreshedHeaders });
}
