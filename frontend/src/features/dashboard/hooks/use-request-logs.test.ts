import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { createElement, type PropsWithChildren, useEffect } from "react";
import {
  MemoryRouter,
  useLocation,
  useNavigate,
  type NavigateFunction,
} from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { requestLogFiltersApplied, useRequestLogs } from "@/features/dashboard/hooks/use-request-logs";
import { server } from "@/test/mocks/server";

function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
    },
  });
}

function LocationSpy({
  onChange,
  onNavigateReady,
}: {
  onChange?: (search: string) => void;
  onNavigateReady?: (navigate: NavigateFunction) => void;
}) {
  const routeLocation = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    onChange?.(routeLocation.search);
  }, [routeLocation.search, onChange]);

  useEffect(() => {
    onNavigateReady?.(navigate);
  }, [navigate, onNavigateReady]);

  return null;
}

function createWrapper(
  queryClient: QueryClient,
  initialEntry = "/dashboard",
  onLocationChange?: (search: string) => void,
  onNavigateReady?: (navigate: NavigateFunction) => void,
) {
  return function Wrapper({ children }: PropsWithChildren) {
    return createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(
        MemoryRouter,
        { initialEntries: [initialEntry] },
        createElement(LocationSpy, { onChange: onLocationChange, onNavigateReady }),
        children,
      ),
    );
  };
}

describe("useRequestLogs", () => {
  it("defaults legacy all-history bookmarks to a bounded timeframe", async () => {
    let location = "";
    const { result } = renderHook(() => useRequestLogs(), {
      wrapper: createWrapper(createTestQueryClient(), "/dashboard?timeframe=all&overviewTimeframe=30d", (search) => { location = search; }),
    });
    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    expect(result.current.listFilters.timeframe).toBe("24h");
    expect(result.current.facetFilters.timeframe).toBe("24h");
    expect(new URLSearchParams(location).get("timeframe")).toBeNull();
    expect(new URLSearchParams(location).get("overviewTimeframe")).toBe("30d");
  });

  it("restores historical UTC ranges and blocks invalid ranges until repaired", async () => {
    const requests: URL[] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({ requests: [], total: 0, hasMore: false });
      }),
      http.get("/api/request-logs/options", ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({ accountIds: [], apiKeys: [], modelOptions: [], statuses: [] });
      }),
    );
    let location = "";
    const params = new URLSearchParams({ since: "2020-01-01T02:00:00+02:00", until: "2020-01-09T00:00:00Z" });
    const { result } = renderHook(() => useRequestLogs(), {
      wrapper: createWrapper(createTestQueryClient(), `/dashboard?${params}`, (search) => { location = search; }),
    });
    expect(result.current.rangeValid).toBe(false);
    expect(requests).toHaveLength(0);
    await act(async () => result.current.updateFilters({ until: "2020-01-08T00:00:00Z" }));
    await waitFor(() => expect(requests).toHaveLength(2));
    for (const request of requests) {
      expect(request.searchParams.get("since")).toBe("2020-01-01T00:00:00.000Z");
      expect(request.searchParams.get("until")).toBe("2020-01-08T00:00:00.000Z");
      expect(request.searchParams.has("timeframe")).toBe(false);
    }
    expect(new URLSearchParams(location).get("until")).toBe("2020-01-08T00:00:00Z");
    await act(async () => result.current.updateFilters({ conversationId: "old-conversation", offset: 0 }));
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(requests[2].searchParams.get("conversation_id")).toBe("old-conversation");
    expect(requests[2].searchParams.get("since")).toBe("2020-01-01T00:00:00.000Z");
    expect(requests[2].searchParams.get("until")).toBe("2020-01-08T00:00:00.000Z");
    await act(async () => result.current.updateFilters({ until: "2019-12-31T00:00:00Z" }));
    expect(result.current.rangeValid).toBe(false);
    expect(requests).toHaveLength(3);
    await act(async () => result.current.updateFilters({ timeframe: "24h", offset: 0 }));
    await waitFor(() => expect(requests).toHaveLength(5));
    expect(result.current.rangeValid).toBe(true);
    expect(new URLSearchParams(location).has("since")).toBe(false);
    expect(new URLSearchParams(location).has("until")).toBe(false);
  });

  it("aborts obsolete searches and sends only settled text while other filters stay immediate", async () => {
    const requests: Array<{ url: URL; signal: AbortSignal }> = [];
    vi.stubGlobal("fetch", vi.fn((url: string, options: RequestInit) => {
      const parsed = new URL(url, "http://localhost");
      if (parsed.pathname.endsWith("/options")) {
        return Promise.resolve(new Response(JSON.stringify({
          accountIds: [], apiKeys: [], modelOptions: [], statuses: [],
        }), { headers: { "Content-Type": "application/json" } }));
      }
      const signal = options.signal as AbortSignal;
      requests.push({ url: parsed, signal });
      return new Promise<Response>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    }));
    const { result, unmount } = renderHook(() => useRequestLogs(), {
      wrapper: createWrapper(createTestQueryClient(), "/dashboard?timeframe=24h"),
    });
    try {
      await waitFor(() => expect(requests).toHaveLength(1));
      vi.useFakeTimers();
      await act(async () => result.current.updateFilters({ search: "q", offset: 0 }));
      expect(requests[0].signal.aborted).toBe(true);
      await act(async () => vi.advanceTimersByTimeAsync(299));
      expect(requests).toHaveLength(1);
      await act(async () => result.current.updateFilters({ search: "", offset: 0 }));
      await act(async () => vi.advanceTimersByTimeAsync(299));
      expect(requests).toHaveLength(1);
      await act(async () => result.current.updateFilters({ search: "quota", offset: 0 }));
      await act(async () => vi.advanceTimersByTimeAsync(299));
      expect(requests).toHaveLength(1);
      await act(async () => vi.advanceTimersByTimeAsync(1));
      expect(requests).toHaveLength(2);
      expect(requests[1].url.searchParams.get("search")).toBe("quota");

      await act(async () => result.current.updateFilters({ offset: 25 }));
      expect(requests).toHaveLength(3);
      expect(requests[1].signal.aborted).toBe(true);
      expect(requests[2].url.searchParams.get("offset")).toBe("25");
    } finally {
      unmount();
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it("keeps symbolic timeframes stable across refetch despite browser clock skew", async () => {
    const listCalls: URLSearchParams[] = [];
    const optionCalls: URLSearchParams[] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        listCalls.push(new URL(request.url).searchParams);
        return HttpResponse.json({ requests: [], total: 0, hasMore: false });
      }),
      http.get("/api/request-logs/options", ({ request }) => {
        optionCalls.push(new URL(request.url).searchParams);
        return HttpResponse.json({
          accountIds: [],
          apiKeys: [],
          modelOptions: [],
          statuses: [],
        });
      }),
    );
    const browserClock = vi.spyOn(Date, "now").mockReturnValue(Date.parse("2036-08-31T12:00:00Z"));

    try {
      const queryClient = createTestQueryClient();
      const wrapper = createWrapper(
        queryClient,
        "/dashboard?timeframe=24h&search=quota&accountId=acc_primary&apiKeyId=key_1&modelOption=gpt-5.1:::high&status=ok&limit=10&offset=20",
      );
      const { result } = renderHook(() => useRequestLogs(), { wrapper });

      await waitFor(() => {
        expect(result.current.logsQuery.isSuccess).toBe(true);
        expect(result.current.optionsQuery.isSuccess).toBe(true);
      });
      await act(async () => {
        await Promise.all([
          result.current.logsQuery.refetch(),
          result.current.optionsQuery.refetch(),
        ]);
      });

      expect(listCalls).toHaveLength(2);
      expect(optionCalls).toHaveLength(2);
      for (const params of [...listCalls, ...optionCalls]) {
        expect(params.get("timeframe")).toBe("24h");
        expect(params.has("since")).toBe(false);
      }
      for (const params of listCalls) {
        expect(params.get("search")).toBe("quota");
        expect(params.getAll("accountId")).toEqual(["acc_primary"]);
        expect(params.getAll("apiKeyId")).toEqual(["key_1"]);
        expect(params.getAll("modelOption")).toEqual(["gpt-5.1:::high"]);
        expect(params.getAll("status")).toEqual(["ok"]);
        expect(params.get("limit")).toBe("10");
        expect(params.get("offset")).toBe("20");
      }
    } finally {
      browserClock.mockRestore();
    }
  });
  it("maps URL params into filter state and query key", async () => {
    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(
      queryClient,
      "/dashboard?overviewTimeframe=30d&search=rate&timeframe=24h&accountId=acc_primary&apiKeyId=key_1&modelOption=gpt-5.1:::high&status=rate_limit&limit=10&offset=20",
    );

    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));

    expect(result.current.filters).toMatchObject({
      search: "rate",
      timeframe: "24h",
      accountIds: ["acc_primary"],
      apiKeyIds: ["key_1"],
      modelOptions: ["gpt-5.1:::high"],
      statuses: ["rate_limit"],
      limit: 10,
      offset: 20,
    });

    const query = queryClient.getQueryCache().findAll({
      queryKey: ["dashboard", "request-logs"],
    })[0];
    const key = query?.queryKey as
      | [string, string, { search: string; limit: number; offset: number }]
      | undefined;
    expect(key?.[2].search).toBe("rate");
    expect(key?.[2].limit).toBe(10);
    expect(key?.[2].offset).toBe(20);
  });

  it("preserves unrelated search params when request-log filters change", async () => {
    const queryClient = createTestQueryClient();
    let locationSearch = "";
    const wrapper = createWrapper(
      queryClient,
      "/dashboard?overviewTimeframe=30d&limit=25&offset=0",
      (search) => {
        locationSearch = search;
      },
    );
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));

    act(() => {
      result.current.updateFilters({ search: "quota" });
    });

    await waitFor(() => expect(result.current.filters.search).toBe("quota"));
    expect(locationSearch).toContain("overviewTimeframe=30d");
    expect(locationSearch).toContain("search=quota");
  });

  it("keeps filtered-empty semantics while clearing a filter", async () => {
    let releaseUnfiltered: (() => void) | undefined;
    const unfilteredResponse = new Promise<void>((resolve) => {
      releaseUnfiltered = resolve;
    });
    server.use(
      http.get("/api/request-logs", async ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("search")) {
          return HttpResponse.json({ requests: [], total: 0, hasMore: false });
        }
        await unfilteredResponse;
        return HttpResponse.json({ requests: [], total: 1, hasMore: false });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?search=missing");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    expect(result.current.emptyStateFiltersApplied).toBe(true);

    act(() => {
      result.current.updateFilters({ search: "", offset: 0 });
    });

    await waitFor(() => expect(result.current.filters.search).toBe(""));
    expect(result.current.logsQuery.isPlaceholderData).toBe(true);
    expect(result.current.logsQuery.data?.total).toBe(0);
    expect(result.current.emptyStateFiltersApplied).toBe(true);

    releaseUnfiltered?.();
    await waitFor(() => expect(result.current.logsQuery.data?.total).toBe(1));
    expect(result.current.emptyStateFiltersApplied).toBe(false);
  });

  it("keeps filtered-empty semantics when route navigation clears filters", async () => {
    let releaseUnfiltered: (() => void) | undefined;
    let navigate: NavigateFunction | undefined;
    const unfilteredResponse = new Promise<void>((resolve) => {
      releaseUnfiltered = resolve;
    });
    server.use(
      http.get("/api/request-logs", async ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("search")) {
          return HttpResponse.json({ requests: [], total: 0, hasMore: false });
        }
        await unfilteredResponse;
        return HttpResponse.json({ requests: [], total: 1, hasMore: false });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(
      queryClient,
      "/dashboard?search=missing",
      undefined,
      (routerNavigate) => {
        navigate = routerNavigate;
      },
    );
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    await waitFor(() => expect(navigate).toBeDefined());

    act(() => {
      navigate?.("/dashboard");
    });

    await waitFor(() => expect(result.current.filters.search).toBe(""));
    expect(result.current.logsQuery.isPlaceholderData).toBe(true);
    expect(result.current.emptyStateFiltersApplied).toBe(true);

    releaseUnfiltered?.();
    await waitFor(() => expect(result.current.logsQuery.data?.total).toBe(1));
    expect(result.current.emptyStateFiltersApplied).toBe(false);
  });

  it("supports pagination updates with total/hasMore response", async () => {
    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?limit=1&offset=0");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    const firstTotal = result.current.logsQuery.data?.total ?? 0;
    expect(typeof result.current.logsQuery.data?.hasMore).toBe("boolean");

    act(() => {
      result.current.updateFilters({ offset: 1 });
    });

    await waitFor(() => {
      expect(result.current.filters.offset).toBe(1);
      expect(result.current.logsQuery.isSuccess).toBe(true);
    });

    expect(result.current.logsQuery.data?.total).toBe(firstTotal);
  });

  it("uses facet filters for options query without status self-filter", async () => {
    const calls: Array<{
      statuses: string[];
      accountIds: string[];
      apiKeyIds: string[];
      modelOptions: string[];
      timeframe: string | null;
      since: string | null;
    }> = [];
    server.use(
      http.get("/api/request-logs/options", ({ request }) => {
        const url = new URL(request.url);
        calls.push({
          statuses: url.searchParams.getAll("status"),
          accountIds: url.searchParams.getAll("accountId"),
          apiKeyIds: url.searchParams.getAll("apiKeyId"),
          modelOptions: url.searchParams.getAll("modelOption"),
          timeframe: url.searchParams.get("timeframe"),
          since: url.searchParams.get("since"),
        });
        return HttpResponse.json({
          accountIds: [],
          apiKeys: [],
          modelOptions: [],
          statuses: ["ok", "rate_limit", "quota", "error"],
        });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(
      queryClient,
      "/dashboard?timeframe=24h&accountId=acc_primary&apiKeyId=key_1&modelOption=gpt-5.1:::high&status=ok",
    );

    const { result } = renderHook(() => useRequestLogs(), { wrapper });
    await waitFor(() => expect(result.current.optionsQuery.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.filters.accountIds).toEqual(["acc_primary"]));
    await waitFor(() => expect(result.current.filters.apiKeyIds).toEqual(["key_1"]));
    await waitFor(() => expect(result.current.filters.modelOptions).toEqual(["gpt-5.1:::high"]));

    const matchingCall = calls.find(
      (call) =>
        call.accountIds.includes("acc_primary") &&
        call.apiKeyIds.includes("key_1") &&
        call.modelOptions.includes("gpt-5.1:::high"),
    );
    expect(matchingCall).toBeDefined();
    expect(matchingCall?.statuses).toEqual([]);
    expect(matchingCall?.timeframe).toBe("24h");
    expect(matchingCall?.since).toBeNull();
  });

  it("removes stale status from request parameters immediately after unselect", async () => {
    const statusesPerCall: string[][] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        const url = new URL(request.url);
        statusesPerCall.push(url.searchParams.getAll("status"));
        return HttpResponse.json({
          requests: [],
          total: 0,
          hasMore: false,
        });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?status=ok&status=stale_status");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.filters.statuses).toEqual(["ok", "stale_status"]));
    await waitFor(() =>
      expect(
        statusesPerCall.some(
          (statuses) => statuses.includes("ok") && statuses.includes("stale_status"),
        ),
      ).toBe(true),
    );

    act(() => {
      result.current.updateFilters({ statuses: ["ok"], offset: 0 });
    });

    await waitFor(() => expect(result.current.filters.statuses).toEqual(["ok"]));
    await waitFor(() => expect(statusesPerCall[statusesPerCall.length - 1]).toEqual(["ok"]));
  });

  it("preserves api key filters in the URL and request-log queries", async () => {
    const apiKeyCalls: string[][] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        const url = new URL(request.url);
        apiKeyCalls.push(url.searchParams.getAll("apiKeyId"));
        return HttpResponse.json({
          requests: [],
          total: 0,
          hasMore: false,
        });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?apiKeyId=key_1");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.filters.apiKeyIds).toEqual(["key_1"]));
    await waitFor(() => expect(apiKeyCalls.some((ids) => ids.includes("key_1"))).toBe(true));

    act(() => {
      result.current.updateFilters({ apiKeyIds: ["key_1", "key_2"], offset: 0 });
    });

    await waitFor(() => expect(result.current.filters.apiKeyIds).toEqual(["key_1", "key_2"]));
    await waitFor(() =>
      expect(apiKeyCalls[apiKeyCalls.length - 1]).toEqual(["key_1", "key_2"]),
    );
  });

  it("parses conversationId from URL and maps to conversation_id in API", async () => {
    const apiCallParams: string[] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        const url = new URL(request.url);
        apiCallParams.push(url.searchParams.get("conversation_id") ?? "");
        return HttpResponse.json({
          requests: [],
          total: 0,
          hasMore: false,
        });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?conversationId=conv_abc123&limit=25&offset=0");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    expect(result.current.filters.conversationId).toBe("conv_abc123");
    expect(apiCallParams.some((p) => p === "conv_abc123")).toBe(true);
  });

  it("maps null/empty conversationId to no conversation_id param in API", async () => {
    const apiCallParams: string[] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        const url = new URL(request.url);
        apiCallParams.push(url.searchParams.get("conversation_id") ?? "-missing-");
        return HttpResponse.json({
          requests: [],
          total: 0,
          hasMore: false,
        });
      }),
    );

    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?limit=25&offset=0");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    expect(result.current.filters.conversationId).toBeNull();
    expect(apiCallParams.some((p) => p === "-missing-")).toBe(true);
  });

  it("resets offset when conversationId is set", async () => {
    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?limit=25&offset=10");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    expect(result.current.filters.offset).toBe(10);

    act(() => {
      result.current.updateFilters({ conversationId: "conv_test", offset: 0 });
    });

    await waitFor(() => {
      expect(result.current.filters.conversationId).toBe("conv_test");
      expect(result.current.filters.offset).toBe(0);
    });
  });

  it("resets offset when conversationId is cleared", async () => {
    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(queryClient, "/dashboard?conversationId=conv_abc123&limit=25&offset=5");
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    expect(result.current.filters.offset).toBe(5);

    act(() => {
      result.current.updateFilters({ conversationId: null, offset: 0 });
    });

    await waitFor(() => {
      expect(result.current.filters.conversationId).toBeNull();
      expect(result.current.filters.offset).toBe(0);
    });
  });

  it("preserves unrelated search params when conversationId is set or cleared", async () => {
    const queryClient = createTestQueryClient();
    let locationSearch = "";
    const wrapper = createWrapper(
      queryClient,
      "/dashboard?overviewTimeframe=30d&limit=25&offset=0",
      (search) => {
        locationSearch = search;
      },
    );
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));

    act(() => {
      result.current.updateFilters({ conversationId: "conv_preserve", offset: 0 });
    });

    await waitFor(() => expect(result.current.filters.conversationId).toBe("conv_preserve"));
    expect(locationSearch).toContain("overviewTimeframe=30d");
    expect(locationSearch).toContain("conversationId=conv_preserve");

    act(() => {
      result.current.updateFilters({ conversationId: null, offset: 0 });
    });

    await waitFor(() => expect(result.current.filters.conversationId).toBeNull());
    expect(locationSearch).toContain("overviewTimeframe=30d");
    expect(locationSearch).not.toContain("conversationId");
  });

  it("writes and clears conversationId in browser URL", async () => {
    const queryClient = createTestQueryClient();
    let locationSearch = "";
    const wrapper = createWrapper(
      queryClient,
      "/dashboard?limit=25&offset=0",
      (search) => {
        locationSearch = search;
      },
    );
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));

    act(() => {
      result.current.updateFilters({ conversationId: "conv_write_clear", offset: 0 });
    });

    await waitFor(() => {
      expect(locationSearch).toContain("conversationId=conv_write_clear");
    });

    act(() => {
      result.current.updateFilters({ conversationId: null, offset: 0 });
    });

    await waitFor(() => {
      expect(locationSearch).not.toContain("conversationId");
    });
  });

  it("round-trips conversation IDs with special characters through URL", async () => {
    const specialIds = [
      "conv/a&b+c",
      "conv has spaces",
      "conv_utf8_\u2603",
    ];
    const queryClient = createTestQueryClient();

    for (const rawId of specialIds) {
      const encoded = encodeURIComponent(rawId);
      const wrapper = createWrapper(
        queryClient,
        `/dashboard?conversationId=${encoded}&limit=25&offset=0`,
      );
      const { result } = renderHook(() => useRequestLogs(), { wrapper });

      await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
      expect(result.current.filters.conversationId).toBe(rawId);
    }
  });

  it("maps special-character conversationId to conversation_id in API", async () => {
    const apiParams: string[] = [];
    server.use(
      http.get("/api/request-logs", ({ request }) => {
        const url = new URL(request.url);
        apiParams.push(url.searchParams.get("conversation_id") ?? "missing");
        return HttpResponse.json({ requests: [], total: 0, hasMore: false });
      }),
    );

    const rawId = "conv/a&b+c";
    const queryClient = createTestQueryClient();
    const wrapper = createWrapper(
      queryClient,
      `/dashboard?conversationId=${encodeURIComponent(rawId)}&limit=25&offset=0`,
    );
    const { result } = renderHook(() => useRequestLogs(), { wrapper });

    await waitFor(() => expect(result.current.logsQuery.isSuccess).toBe(true));
    const decoded = result.current.filters.conversationId;
    expect(decoded).toBe(rawId);
    expect(apiParams.some((p) => p === rawId)).toBe(true);
  });
});

describe("requestLogFiltersApplied", () => {
  const defaults = {
    search: "",
    timeframe: "24h" as const,
    accountIds: [],
    apiKeyIds: [],
    modelOptions: [],
    statuses: [],
    conversationId: null,
    limit: 25,
    offset: 0,
  };

  it("is false for default request-log filters", () => {
    expect(requestLogFiltersApplied(defaults)).toBe(false);
  });

  it("is true when a narrowing filter is set", () => {
    expect(requestLogFiltersApplied({ ...defaults, timeframe: "7d" })).toBe(true);
    expect(requestLogFiltersApplied({ ...defaults, search: "rate" })).toBe(true);
    expect(requestLogFiltersApplied({ ...defaults, conversationId: "conv-1" })).toBe(true);
    expect(requestLogFiltersApplied({ ...defaults, statuses: ["error"] })).toBe(true);
  });
});
