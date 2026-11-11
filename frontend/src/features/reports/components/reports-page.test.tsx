import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { renderWithProviders } from "@/test/utils";
import type {
  ReportsResponse,
  ThreadIdentityFacet,
  ThreadIdentityResponse,
} from "@/features/reports/schemas";
import { listAccounts } from "@/features/accounts/api";
import { getBrowserReportsTimeZone } from "@/features/reports/date";
import {
  useReports,
  useReportsOptions,
  useReportSpeeds,
  useThreadIdentity,
} from "@/features/reports/hooks/use-reports";
import {
  REPORT_CHART_DEFINITIONS,
  REPORT_CHART_VISIBILITY_STORAGE_KEY,
} from "@/features/reports/hooks/use-report-chart-visibility";
import { ReportsPage } from "./reports-page";

vi.mock("@/features/accounts/api", () => ({
  listAccounts: vi.fn().mockResolvedValue({ accounts: [] }),
}));

vi.mock("@/features/reports/hooks/use-reports", () => ({
  useReports: vi.fn(),
  useReportsOptions: vi.fn(),
  useReportSpeeds: vi.fn(),
  useThreadIdentity: vi.fn(),
}));

vi.mock("@/features/reports/date", async () => {
  const actual = await vi.importActual<typeof import("@/features/reports/date")>(
    "@/features/reports/date",
  );
  return {
    ...actual,
    getBrowserReportsTimeZone: vi.fn(),
  };
});

vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => (
      <div style={{ width: 400, height: 200 }}>{children}</div>
    ),
  };
});

const EMPTY_REPORT: ReportsResponse = {
  summary: {
    totalCostUsd: 0,
    totalInputTokens: 0,
    totalOutputTokens: 0,
    totalReasoningTokens: 0,
    reasoningUsageKnownRequests: 0,
    totalCachedTokens: 0,
    totalRequests: 0,
    totalCancelled: 0,
    totalErrors: 0,
    totalConversations: 0,
    activeAccounts: 0,
    avgCostPerDay: 0,
    avgRequestsPerDay: 0,
  },
  comparison: {
    canCompare: false,
    previous: {
      totalCostUsd: 0,
      totalTokens: 0,
      totalRequests: 0,
    },
  },
  daily: [],
  byModel: [],
  byUseragent: [],
  byAccount: [],
};

const EMPTY_THREAD_IDENTITY_FACET: ThreadIdentityFacet = {
  requests: 0,
  requestShare: 0,
  unattributedRequestShare: 0,
  conversations: 0,
  meanAccountsPerConversation: 0,
  singleAccountConversationShare: 0,
  turns: 0,
  accountSwitchRate: 0,
  cacheHitRatio: 0,
  cacheSampleInputTokens: 0,
  threadGroupingApproximate: false,
};

const useReportsOptionsMock = vi.mocked(useReportsOptions);
const useReportsMock = vi.mocked(useReports);
const useReportSpeedsMock = vi.mocked(useReportSpeeds);
const useThreadIdentityMock = vi.mocked(useThreadIdentity);
const listAccountsMock = vi.mocked(listAccounts);
const getBrowserReportsTimeZoneMock = vi.mocked(getBrowserReportsTimeZone);
type UseReportsMockResult = ReturnType<typeof useReports>;
const REPORTS_TIMEZONE_STORAGE_KEY = "codex-lb-reports-timezone";
// Derived so adding a card to REPORT_CHART_DEFINITIONS does not leave these
// assertions checking a stale count.
const ALL_CHARTS_BUTTON = `Charts (${REPORT_CHART_DEFINITIONS.length})`;
const THREAD_IDENTITY: ThreadIdentityResponse = {
  available: true,
  maxDays: 7,
  windowDays: 7,
  conversationMinRequests: 3,
  switchMaxGapSeconds: 600,
  cacheMinInputTokens: 5000,
  totalRequests: 0,
  unkeyedRequestShare: 0,
  keyed: EMPTY_THREAD_IDENTITY_FACET,
  unkeyed: { ...EMPTY_THREAD_IDENTITY_FACET, threadGroupingApproximate: true },
};

const asUseReportsResult = (
  value: Partial<UseReportsMockResult>,
): UseReportsMockResult => value as unknown as UseReportsMockResult;

describe("ReportsPage", () => {
  beforeEach(() => {
    useReportsMock.mockReset();
    useReportSpeedsMock.mockReset();
    useReportSpeedsMock.mockReturnValue({
      data: { available: true, maxDays: 7, daily: [] }, error: null,
      isLoading: false, isFetching: false, refetch: vi.fn(),
    } as unknown as ReturnType<typeof useReportSpeeds>);
    useReportsOptionsMock.mockReset();
    useThreadIdentityMock.mockReset();
    useThreadIdentityMock.mockReturnValue({
      data: THREAD_IDENTITY,
      error: null,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useThreadIdentity>);
    useReportsOptionsMock.mockReturnValue({ data: { models: ["gpt-5.1", "gpt-5.2"], useragents: ["CLI", "SDK"] }, isLoading: false, refetch: vi.fn() } as unknown as ReturnType<typeof useReportsOptions>);
    listAccountsMock.mockReset();
    getBrowserReportsTimeZoneMock.mockReset();
    window.localStorage.clear();
    listAccountsMock.mockResolvedValue({ accounts: [] });
    getBrowserReportsTimeZoneMock.mockReturnValue("America/Los_Angeles");
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it("initializes default dates when the reports page mounts", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2030-01-15T12:00:00Z"));
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(<ReportsPage />);

    expect(useReportsMock.mock.calls[0]?.[0]).toMatchObject({
      startDate: "2030-01-09",
      endDate: "2030-01-15",
    });
  });

  it("passes the page-managed timezone state into the report and options queries", () => {
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(<ReportsPage />);

    expect(useReportsMock).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        startDate: expect.any(String),
        endDate: expect.any(String),
      }),
      "America/Los_Angeles",
    );
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        startDate: expect.any(String),
        endDate: expect.any(String),
        model: "",
        useragent: "",
      }),
      "America/Los_Angeles",
    );
  });

  it("uses the live valid timezone for reports queries even when a cached value exists", async () => {
    window.localStorage.setItem(REPORTS_TIMEZONE_STORAGE_KEY, "UTC");
    getBrowserReportsTimeZoneMock.mockReset();
    const actualDate = await vi.importActual<typeof import("@/features/reports/date")>(
      "@/features/reports/date",
    );
    getBrowserReportsTimeZoneMock.mockImplementation(actualDate.getBrowserReportsTimeZone);
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      locale: "en-US",
      calendar: "gregory",
      numberingSystem: "latn",
      timeZone: "America/Los_Angeles",
    });

    renderWithProviders(<ReportsPage />);

    expect(useReportsMock).toHaveBeenNthCalledWith(1, expect.any(Object), "America/Los_Angeles");
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(1, expect.any(Object), "America/Los_Angeles");
  });

  it("uses the cached valid timezone for reports queries when live detection is unavailable", async () => {
    window.localStorage.setItem(REPORTS_TIMEZONE_STORAGE_KEY, "Europe/Paris");
    getBrowserReportsTimeZoneMock.mockReset();
    const actualDate = await vi.importActual<typeof import("@/features/reports/date")>(
      "@/features/reports/date",
    );
    getBrowserReportsTimeZoneMock.mockImplementation(actualDate.getBrowserReportsTimeZone);
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      locale: "en-US",
      calendar: "gregory",
      numberingSystem: "latn",
      timeZone: undefined as unknown as string,
    });

    renderWithProviders(<ReportsPage />);

    expect(useReportsMock).toHaveBeenNthCalledWith(1, expect.any(Object), "Europe/Paris");
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(1, expect.any(Object), "Europe/Paris");
  });

  it("omits timezone for reports queries only when live and cached timezones are both invalid", async () => {
    window.localStorage.setItem(REPORTS_TIMEZONE_STORAGE_KEY, "Moon/BaseAlpha");
    getBrowserReportsTimeZoneMock.mockReset();
    const actualDate = await vi.importActual<typeof import("@/features/reports/date")>(
      "@/features/reports/date",
    );
    getBrowserReportsTimeZoneMock.mockImplementation(actualDate.getBrowserReportsTimeZone);
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      locale: "en-US",
      calendar: "gregory",
      numberingSystem: "latn",
      timeZone: "Mars/Olympus",
    });

    renderWithProviders(<ReportsPage />);

    expect(useReportsMock).toHaveBeenNthCalledWith(1, expect.any(Object), undefined);
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(1, expect.any(Object), undefined);
  });

  it("refreshes timezone state on focus, visibility changes, and interval ticks", async () => {
    vi.useFakeTimers();
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );
    getBrowserReportsTimeZoneMock.mockReturnValueOnce("America/Los_Angeles");

    renderWithProviders(<ReportsPage />);

    expect(useReportsMock).toHaveBeenNthCalledWith(
      1,
      expect.any(Object),
      "America/Los_Angeles",
    );
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(
      1,
      expect.any(Object),
      "America/Los_Angeles",
    );

    getBrowserReportsTimeZoneMock.mockReturnValue("America/New_York");
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(useReportsMock).toHaveBeenNthCalledWith(
      2,
      expect.any(Object),
      "America/New_York",
    );
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(
      2,
      expect.any(Object),
      "America/New_York",
    );

    getBrowserReportsTimeZoneMock.mockReturnValue(undefined);
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    expect(useReportsMock).toHaveBeenNthCalledWith(
      3,
      expect.any(Object),
      undefined,
    );
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(
      3,
      expect.any(Object),
      undefined,
    );

    getBrowserReportsTimeZoneMock.mockReturnValue("America/Chicago");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(useReportsMock).toHaveBeenNthCalledWith(
      4,
      expect.any(Object),
      "America/Chicago",
    );
    expect(useReportsOptionsMock).toHaveBeenNthCalledWith(
      4,
      expect.any(Object),
      "America/Chicago",
    );
  });

  it("keeps model options from the unfiltered model catalog", async () => {
    const user = userEvent.setup();
    useReportsMock.mockImplementation((filters) =>
      asUseReportsResult({
        data: {
          ...EMPTY_REPORT,
          byModel: filters.model
            ? [{ model: "gpt-5.1", costUsd: 1, requests: 1, percentage: 100 }]
              : [
                  { model: "gpt-5.1", costUsd: 1, requests: 1, percentage: 50 },
                  { model: "gpt-5.2", costUsd: 1, requests: 1, percentage: 50 },
                ],
          byUseragent: filters.model
            ? [{ useragent: "CLI", costUsd: 1, requests: 1, percentage: 100 }]
            : [
                { useragent: "CLI", costUsd: 1, requests: 1, percentage: 50 },
                { useragent: "SDK", costUsd: 1, requests: 1, percentage: 50 },
              ],
        },
        isLoading: false,
      }),
    );

    renderWithProviders(<ReportsPage initialFilters={{ model: "gpt-5.1" }} />);

    await user.click(
      screen.getByRole("button", { name: /gpt-5.1/i, expanded: false }),
    );

    expect(
      await screen.findByRole("menuitemcheckbox", { name: /gpt-5.2/i }),
    ).toBeInTheDocument();
  });

  it("keeps user-agent options from the relaxed reports catalog", async () => {
    const user = userEvent.setup();
    useReportsMock.mockImplementation((filters) =>
      asUseReportsResult({
        data: {
          ...EMPTY_REPORT,
          byModel: [
            { model: "gpt-5.1", costUsd: 1, requests: 1, percentage: 100 },
          ],
          byUseragent: filters.useragent
            ? [{ useragent: "CLI", costUsd: 1, requests: 1, percentage: 100 }]
            : [
                { useragent: "CLI", costUsd: 1, requests: 1, percentage: 50 },
                { useragent: "SDK", costUsd: 1, requests: 1, percentage: 50 },
              ],
        },
        isLoading: false,
      }),
    );

    renderWithProviders(<ReportsPage initialFilters={{ useragent: "CLI" }} />);

    await user.click(
      screen.getByRole("button", { name: /^CLI$/i, expanded: false }),
    );

    expect(
      await screen.findByRole("menuitemcheckbox", { name: /^SDK$/i }),
    ).toBeInTheDocument();
  });

  it("clears the last clicked preset highlight after manual date edits", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2030-01-15T12:00:00Z"));

    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    const { container } = renderWithProviders(<ReportsPage />);

    const button7d = screen.getByRole("button", { name: "7d" });
    const button30d = screen.getByRole("button", { name: "30d" });

    expect(button7d).toHaveAttribute("aria-pressed", "true");
    expect(button30d).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(button30d);

    expect(button30d).toHaveAttribute("aria-pressed", "true");
    expect(useReportsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        startDate: "2029-12-17",
        endDate: "2030-01-15",
      }),
      "America/Los_Angeles",
    );

    const [startDateInput] = container.querySelectorAll<HTMLInputElement>('input[type="date"]');
    fireEvent.change(startDateInput, { target: { value: "2030-01-01" } });

    expect(button30d).toHaveAttribute("aria-pressed", "false");
    expect(useReportsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        startDate: "2030-01-01",
        endDate: "2030-01-15",
      }),
      "America/Los_Angeles",
    );
  });

  it("shows an error when report loading fails", async () => {
    useReportsMock.mockImplementation((filters) =>
      filters.model
        ? asUseReportsResult({
            isLoading: false,
            isError: true,
            error: new Error("report API unavailable"),
            refetch: vi.fn(),
            data: null as unknown as ReportsResponse,
          })
        : asUseReportsResult({
            data: EMPTY_REPORT,
            isLoading: false,
            isError: false,
            refetch: vi.fn(),
          }),
    );

    renderWithProviders(<ReportsPage initialFilters={{ model: "gpt-5.1" }} />);

    expect(
      await screen.findByText(
        /Failed to load report data: report API unavailable/i,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });

  it("shows one shared catalog failure for model and user-agent options", async () => {
    useReportsOptionsMock.mockReturnValue({ isLoading: false, error: new Error("shared catalog endpoint unavailable"), refetch: vi.fn() } as unknown as ReturnType<typeof useReportsOptions>);
    useReportsMock.mockImplementation((filters) =>
      filters.model || filters.useragent
        ? asUseReportsResult({
            data: {
              ...EMPTY_REPORT,
              byModel: [
                { model: "gpt-5.1", costUsd: 1, requests: 1, percentage: 100 },
              ],
              byUseragent: [
                { useragent: "CLI", costUsd: 1, requests: 1, percentage: 100 },
              ],
            },
            isLoading: false,
            isError: false,
            refetch: vi.fn(),
          })
        : asUseReportsResult({
            isLoading: false,
            isError: true,
            error: new Error("shared catalog endpoint unavailable"),
            refetch: vi.fn(),
            data: undefined,
          }),
    );

    renderWithProviders(
      <ReportsPage initialFilters={{ model: "gpt-5.1", useragent: "CLI" }} />,
    );

    expect(
      await screen.findByText(
        /Failed to load model and user-agent options: shared catalog endpoint unavailable/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /gpt-5.1/i, expanded: false }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^CLI$/i, expanded: false }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Failed to load model options:/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Failed to load user-agent options:/i),
    ).not.toBeInTheDocument();
  });

  it("renders a saved chart subset without changing reports query inputs", async () => {
    window.localStorage.setItem(
      REPORT_CHART_VISIBILITY_STORAGE_KEY,
      JSON.stringify(["costByDay"]),
    );
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(<ReportsPage />);

    expect(useReportSpeedsMock.mock.calls.at(-1)?.[2]).toBe(false);

    for (const [filters] of useReportsMock.mock.calls) {
      expect(filters).not.toHaveProperty("visibleChartIds");
    }
  });

  it("only queries thread identity while its card is visible", async () => {
    const user = userEvent.setup();
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(<ReportsPage />);

    expect(await screen.findByTestId("thread-identity-card")).toBeInTheDocument();
    expect(useThreadIdentityMock.mock.calls.at(-1)?.[2]).toBe(true);

    await user.click(screen.getByRole("button", { name: ALL_CHARTS_BUTTON }));
    await user.click(
      screen.getByRole("menuitemcheckbox", { name: "Thread Identity & Cache Locality" }),
    );
    await user.keyboard("{Escape}");

    expect(screen.queryByTestId("thread-identity-card")).not.toBeInTheDocument();
    expect(useThreadIdentityMock.mock.calls.at(-1)?.[2]).toBe(false);
  });

  it("keeps summaries and non-line charts when every line chart is hidden", async () => {
    const user = userEvent.setup();
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(<ReportsPage />);

    await user.click(screen.getByRole("button", { name: ALL_CHARTS_BUTTON }));
    for (const chartOption of screen.getAllByRole("menuitemcheckbox")) {
      await user.click(chartOption);
    }
    await user.keyboard("{Escape}");

    expect(useReportSpeedsMock.mock.calls.at(-1)?.[2]).toBe(false);
    expect(window.localStorage.getItem(REPORT_CHART_VISIBILITY_STORAGE_KEY)).toBe(
      JSON.stringify([]),
    );
  });

  it("does not change reports query calls when chart visibility changes", async () => {
    const user = userEvent.setup();
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(
      <ReportsPage initialFilters={{ model: "gpt-5.1", useragent: "CLI" }} />,
    );

    await screen.findByText("Cost by Day");
    const callCountBeforeToggle = useReportsMock.mock.calls.length;
    const callsBeforeToggle = useReportsMock.mock.calls.slice(-1).map(
      ([filters, timeZone]) => [filters, timeZone],
    );
    await user.click(screen.getByRole("button", { name: ALL_CHARTS_BUTTON }));
    await user.click(
      screen.getByRole("menuitemcheckbox", { name: "Queue Wait" }),
    );

    expect(useReportsMock.mock.calls.length).toBeGreaterThan(callCountBeforeToggle);
    expect(useReportsMock.mock.calls.slice(-1)).toEqual(callsBeforeToggle);
  });

  it("keeps the model and user-agent metric toggles independent", async () => {
    const user = userEvent.setup();

    useReportsMock.mockImplementation(() =>
      asUseReportsResult({
        data: {
          ...EMPTY_REPORT,
          byModel: [
            { model: "gpt-5.1", costUsd: 12, requests: 3, percentage: 100 },
          ],
          byUseragent: [
            { useragent: "CLI", costUsd: 10, requests: 2, percentage: 100 },
          ],
        },
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(<ReportsPage />);

    const modelCard = (await screen.findByText("Distribution by Model")).closest("div.rounded-xl.border.bg-card.p-5");
    const useragentCard = (await screen.findByText("Distribution by UserAgent")).closest("div.rounded-xl.border.bg-card.p-5");

    expect(modelCard).not.toBeNull();
    expect(useragentCard).not.toBeNull();

    await user.click(within(useragentCard as HTMLElement).getByRole("button", { name: /^req$/i }));

    expect(within(modelCard as HTMLElement).getByRole("button", { name: /^cost$/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(useragentCard as HTMLElement).getByRole("button", { name: /^req$/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows account option load failures with a retry button and recovers when retried", async () => {
    const user = userEvent.setup();
    useReportsMock.mockImplementation(() =>
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );
    listAccountsMock
      .mockRejectedValueOnce(new Error("accounts backend timeout"))
      .mockResolvedValueOnce({ accounts: [] });

    renderWithProviders(<ReportsPage />);

    const accountErrorText = await screen.findByText(
      /Failed to load account options: accounts backend timeout/i,
    );
    expect(accountErrorText).toBeInTheDocument();
    expect(
      screen
        .getAllByRole("button", { name: /accounts/i })
        .find((button) => button.getAttribute("aria-haspopup") === "menu"),
    ).toBeInTheDocument();

    const accountErrorContainer = accountErrorText.parentElement!.parentElement!;
    const retryButton = within(accountErrorContainer).getByRole("button", { name: /retry/i });
    await user.click(retryButton);

    expect(listAccountsMock).toHaveBeenCalledTimes(2);
    await waitFor(() => {
      expect(
        screen.queryByText(/Failed to load account options:/i),
      ).not.toBeInTheDocument();
    });
  });

  it("forwards initial apiKeyId filter to useReports hook calls", () => {
    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(
      <ReportsPage initialFilters={{ apiKeyId: ["key-123"] }} />,
    );

    expect(useReportsMock).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        apiKeyId: ["key-123"],
      }),
      "America/Los_Angeles",
    );
  });

  it("selects historical API keys from report options including deleted IDs", async () => {
    const user = userEvent.setup();
    useReportsMock.mockImplementation(() =>
      asUseReportsResult({
        data: EMPTY_REPORT,
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );
    useReportsOptionsMock.mockReturnValue({
      data: { models: [], useragents: [], apiKeys: [
        { id: "key_active", name: "Active Key", keyPrefix: "sk-active" },
        { id: "key_deleted", name: "key_deleted", keyPrefix: null },
      ],
      },
      isLoading: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useReportsOptions>);

    renderWithProviders(<ReportsPage />);

    const trigger = await screen.findByRole("button", { name: /api keys/i });
    await user.click(trigger);

    await user.click(await screen.findByRole("menuitemcheckbox", { name: "key_deleted" }));
    await waitFor(() => expect(useReportsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ apiKeyId: ["key_deleted"] }),
      "America/Los_Angeles",
    ));
  });

  it("exports CSV based on the filtered report dataset when API key filter is active", async () => {
    const user = userEvent.setup();
    const blobText = vi.fn(async () => "");
    const createObjectURL = vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      if (!(blob instanceof Blob)) {
        throw new TypeError("expected Blob export payload");
      }
      blobText.mockImplementation(() => blob.text());
      return "blob:daily-breakdown";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    useReportsMock.mockReturnValue(
      asUseReportsResult({
        data: {
          ...EMPTY_REPORT,
          daily: [
            {
              date: "2030-01-15",
              requests: 42,
              conversations: 2,
              inputTokens: 1000,
              outputTokens: 200,
              reasoningTokens: 0,
              cachedInputTokens: 50,
              costUsd: 0.15,
              activeAccounts: 1,
              cancelledCount: 0,
              errorCount: 0,
              medianTtftMs: 0,
              medianTps: 0,
              medianQueueMs: 0,
            },
          ],
        },
        isLoading: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );

    renderWithProviders(
      <ReportsPage
        initialFilters={{
          startDate: "2030-01-15",
          endDate: "2030-01-15",
          apiKeyId: ["key-filtered"],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: /csv/i }));

    expect(createObjectURL).toHaveBeenCalledOnce();
    const csvContent = await blobText();
    expect(csvContent).toContain("2030-01-15,42,2,1000,200,0,50,0.1500,1,0,0");
  });
  it("retries speed failures without reloading successful cost totals", async () => {
    const retryTotals = vi.fn();
    const retrySpeeds = vi.fn();
    useReportsMock.mockReturnValue(asUseReportsResult({
      data: EMPTY_REPORT, isLoading: false, refetch: retryTotals,
    }));
    useReportSpeedsMock.mockReturnValue({
      error: new Error("Speed query timed out"), isFetching: false, refetch: retrySpeeds,
    } as unknown as ReturnType<typeof useReportSpeeds>);
    const user = userEvent.setup();
    renderWithProviders(<ReportsPage />);
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    expect(retrySpeeds).toHaveBeenCalledOnce();
    expect(retryTotals).not.toHaveBeenCalled();
    // Totals remain interactive while the independent speed query fails.
    expect(screen.getByRole("button", { name: /csv/i })).toBeEnabled();
  });

  it("does not request speed data when all speed charts are hidden", () => {
    window.localStorage.setItem(REPORT_CHART_VISIBILITY_STORAGE_KEY, JSON.stringify(["costByDay"]));
    useReportsMock.mockReturnValue(asUseReportsResult({
      data: EMPTY_REPORT, isLoading: false, refetch: vi.fn(),
    }));
    renderWithProviders(<ReportsPage />);
    expect(useReportSpeedsMock.mock.calls.at(-1)?.[2]).toBe(false);
  });

});
