import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type PropsWithChildren } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  useAccounts,
  useAccountTrends,
  useAccountUsageResetCredits,
  useRateLimitResetCredits,
} from "@/features/accounts/hooks/use-accounts";
import { useConversationDetails } from "@/features/dashboard/hooks/use-conversation-details";
import { useConversations } from "@/features/dashboard/hooks/use-conversations";
import { useDashboard, useDashboardProjections } from "@/features/dashboard/hooks/use-dashboard";
import { useRequestLogs } from "@/features/dashboard/hooks/use-request-logs";
import { useReports, useReportsOptions, useThreadIdentity } from "@/features/reports/hooks/use-reports";
import { createDashboardOverview } from "@/test/mocks/factories";

function createWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return function Wrapper({ children }: PropsWithChildren) {
    return createElement(QueryClientProvider, { client }, createElement(MemoryRouter, null, children));
  };
}

type PendingRequest = { url: string; signal: AbortSignal };

function capturePendingRequests() {
  const requests: PendingRequest[] = [];
  vi.stubGlobal("fetch", vi.fn((url: string, options: RequestInit) => {
    const signal = options.signal;
    if (!signal) throw new Error("Query did not pass its abort signal");
    requests.push({ url, signal });
    return new Promise<Response>((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    });
  }));
  return requests;
}

function usePageReads() {
  useDashboard("1d");
  useDashboardProjections();
  useRequestLogs();
  useConversations();
  useConversationDetails("conversation-1");
  const filters = { startDate: "2026-01-01", endDate: "2026-01-01", accountId: [], model: undefined };
  useReports(filters, "UTC");
  useReportsOptions(filters, "UTC");
  useThreadIdentity(filters, "UTC", true);
  useAccounts();
  useAccountTrends("account-1");
  useAccountUsageResetCredits("account-1");
  useRateLimitResetCredits("account-1", true);
}

afterEach(() => vi.unstubAllGlobals());

describe("dashboard read cancellation", () => {
  it("aborts pending page reads when their last observer unmounts", async () => {
    const requests = capturePendingRequests();
    const { unmount } = renderHook(usePageReads, { wrapper: createWrapper() });
    try {
      await waitFor(() => expect(requests).toHaveLength(13));
      expect(requests.every(({ signal }) => !signal.aborted)).toBe(true);
      unmount();
      expect(requests.every(({ signal }) => signal.aborted)).toBe(true);
    } finally {
      unmount();
    }
  });

  it("aborts an obsolete timeframe and accepts the replacement response", async () => {
    const previous: { signal?: AbortSignal } = {};
    vi.stubGlobal("fetch", vi.fn((url: string, options: RequestInit) => {
      if (url.includes("timeframe=1d")) {
        previous.signal = options.signal as AbortSignal;
        return new Promise<Response>((_resolve, reject) => {
          previous.signal?.addEventListener("abort", () => reject(previous.signal?.reason), { once: true });
        });
      }
      return Promise.resolve(new Response(JSON.stringify(createDashboardOverview()), {
        headers: { "Content-Type": "application/json" },
      }));
    }));
    const { result, rerender, unmount } = renderHook(
      ({ timeframe }: { timeframe: "1d" | "7d" }) => useDashboard(timeframe),
      { initialProps: { timeframe: "1d" }, wrapper: createWrapper() },
    );
    try {
      await waitFor(() => expect(previous.signal).toBeDefined());
      rerender({ timeframe: "7d" });
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(previous.signal?.aborted).toBe(true);
      expect(result.current.error).toBeNull();
    } finally {
      unmount();
    }
  });

  it("keeps a shared request alive until its last observer leaves", async () => {
    const requests = capturePendingRequests();
    const wrapper = createWrapper();
    const first = renderHook(() => useDashboard("1d"), { wrapper });
    const second = renderHook(() => useDashboard("1d"), { wrapper });
    try {
      await waitFor(() => expect(requests).toHaveLength(1));
      first.unmount();
      expect(requests[0].signal.aborted).toBe(false);
      second.unmount();
      expect(requests[0].signal.aborted).toBe(true);
    } finally {
      first.unmount();
      second.unmount();
    }
  });
});
