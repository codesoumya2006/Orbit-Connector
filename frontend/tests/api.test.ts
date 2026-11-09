import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const firebaseMock = vi.hoisted(() => ({
  getCurrentUser: vi.fn(),
}));

vi.mock("@/lib/firebase", () => ({
  auth: {
    get currentUser() {
      return firebaseMock.getCurrentUser();
    },
  },
}));

import { apiFetch } from "@/lib/api";

describe("apiFetch", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let getIdToken: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    getIdToken = vi.fn().mockResolvedValue("initial-id-token");
    firebaseMock.getCurrentUser.mockReturnValue({ getIdToken });

    fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("sends the current Firebase user's ID token as a Bearer token", async () => {
    await apiFetch("http://api.test/resource");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const requestInit = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(requestInit.headers).get("Authorization")).toBe(
      "Bearer initial-id-token",
    );
  });

  it("refreshes the ID token and retries once after a 401", async () => {
    getIdToken
      .mockResolvedValueOnce("expired-id-token")
      .mockResolvedValueOnce("refreshed-id-token");
    fetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));

    const response = await apiFetch("http://api.test/resource");

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(getIdToken).toHaveBeenNthCalledWith(2, true);
    expect(
      new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization"),
    ).toBe("Bearer expired-id-token");
    expect(
      new Headers(fetchMock.mock.calls[1][1].headers).get("Authorization"),
    ).toBe("Bearer refreshed-id-token");
  });

  it("does not retry again when the refreshed request also returns 401", async () => {
    getIdToken
      .mockResolvedValueOnce("expired-id-token")
      .mockResolvedValueOnce("refreshed-id-token");
    fetchMock.mockResolvedValue(new Response(null, { status: 401 }));

    const response = await apiFetch("http://api.test/resource");

    expect(response.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(getIdToken).toHaveBeenCalledTimes(2);
    expect(getIdToken).toHaveBeenNthCalledWith(2, true);
  });
});
