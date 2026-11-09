import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  getRequestLogOptions,
  getRequestLogs,
  type RequestLogFacetFilters,
  type RequestLogsListFilters,
} from "@/features/dashboard/api";
import { FilterStateSchema, type FilterState } from "@/features/dashboard/schemas";
import { isRequestLogRangeValid, requestLogTimestamp } from "@/features/dashboard/request-log-window";

const DEFAULT_FILTER_STATE: FilterState = {
  search: "",
  timeframe: "24h",
  accountIds: [],
  apiKeyIds: [],
  modelOptions: [],
  statuses: [],
  conversationId: null,
  limit: 25,
  offset: 0,
};

export function requestLogFiltersApplied(filters: FilterState): boolean {
  return (
    filters.search.trim() !== "" ||
    filters.timeframe !== DEFAULT_FILTER_STATE.timeframe ||
    filters.accountIds.length > 0 ||
    filters.apiKeyIds.length > 0 ||
    filters.modelOptions.length > 0 ||
    filters.statuses.length > 0 ||
    Boolean(filters.conversationId)
  );
}

const REQUEST_LOG_PARAM_KEYS = [
  "search",
  "timeframe",
  "since",
  "until",
  "accountId",
  "apiKeyId",
  "modelOption",
  "status",
  "conversationId",
  "limit",
  "offset",
] as const;

function parseNumber(value: string | null, fallback: number): number {
  if (value === null) {
    return fallback;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseFilterState(params: URLSearchParams): FilterState {
  const candidate = {
    search: params.get("search") ?? "",
    timeframe: params.has("since") || params.has("until")
      ? "custom"
      : params.get("timeframe") === "all" ? "24h" : params.get("timeframe") ?? "24h",
    since: params.get("since") ?? undefined,
    until: params.get("until") ?? undefined,
    accountIds: params.getAll("accountId"),
    apiKeyIds: params.getAll("apiKeyId"),
    modelOptions: params.getAll("modelOption"),
    statuses: params.getAll("status"),
    conversationId: params.get("conversationId") || null,
    limit: parseNumber(params.get("limit"), DEFAULT_FILTER_STATE.limit),
    offset: parseNumber(params.get("offset"), DEFAULT_FILTER_STATE.offset),
  };
  const parsed = FilterStateSchema.safeParse(candidate);
  if (parsed.success) {
    return parsed.data;
  }
  return DEFAULT_FILTER_STATE;
}

function writeFilterState(state: FilterState, base?: URLSearchParams): URLSearchParams {
  const params = new URLSearchParams(base);
  for (const key of REQUEST_LOG_PARAM_KEYS) {
    params.delete(key);
  }
  if (state.search) {
    params.set("search", state.search);
  }
  if (state.timeframe === "custom") {
    params.set("timeframe", "custom");
    if (state.since) params.set("since", state.since);
    if (state.until) params.set("until", state.until);
  } else if (state.timeframe !== DEFAULT_FILTER_STATE.timeframe) {
    params.set("timeframe", state.timeframe);
  }
  for (const value of state.accountIds) {
    params.append("accountId", value);
  }
  for (const value of state.apiKeyIds) {
    params.append("apiKeyId", value);
  }
  for (const value of state.modelOptions) {
    params.append("modelOption", value);
  }
  for (const value of state.statuses) {
    params.append("status", value);
  }
  if (state.conversationId) {
    params.set("conversationId", state.conversationId);
  }
  params.set("limit", String(state.limit));
  params.set("offset", String(state.offset));
  return params;
}

export type UseRequestLogsOptions = {
  enabled?: boolean;
  /**
   * Set false for read-only sessions: the API-key filter control is hidden for
   * them, so any `apiKeyId` carried by the URL (bookmark, or an admin's
   * selection retained across logout) is ignored and removed instead of being
   * sent as an invisible restriction.
   */
  allowApiKeyFilters?: boolean;
};

export function useRequestLogs(options: UseRequestLogsOptions = {}) {
  const enabled = options.enabled ?? true;
  const allowApiKeyFilters = options.allowApiKeyFilters ?? true;
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo(() => {
    const parsed = parseFilterState(searchParams);
    if (allowApiKeyFilters || parsed.apiKeyIds.length === 0) {
      return parsed;
    }
    return { ...parsed, apiKeyIds: [] };
  }, [allowApiKeyFilters, searchParams]);
  const hasLegacyTimeframe = searchParams.get("timeframe") === "all";
  const hasHiddenApiKeyParams = !allowApiKeyFilters && searchParams.has("apiKeyId");
  useEffect(() => {
    if (!hasHiddenApiKeyParams && !hasLegacyTimeframe) {
      return;
    }
    setSearchParams(writeFilterState(filters, searchParams), { replace: true });
  }, [filters, hasHiddenApiKeyParams, hasLegacyTimeframe, searchParams, setSearchParams]);
  // Change the query key immediately to retire the previous read, then admit
  // the new search only after typing settles, including a return to older text.
  const [searchAdmission, setSearchAdmission] = useState({ search: filters.search, settled: true });
  if (searchAdmission.search !== filters.search) {
    setSearchAdmission({ search: filters.search, settled: false });
  }
  useEffect(() => {
    if (searchAdmission.settled) return;
    const timer = window.setTimeout(() => setSearchAdmission({ ...searchAdmission, settled: true }), 300);
    return () => window.clearTimeout(timer);
  }, [searchAdmission]);

  const filtersApplied = requestLogFiltersApplied(filters);
  const rangeValid = filters.timeframe !== "custom" || isRequestLogRangeValid(filters.since, filters.until);
  const timeframe = filters.timeframe === "custom" ? undefined : filters.timeframe;
  const since = filters.timeframe === "custom" ? requestLogTimestamp(filters.since) : undefined;
  const until = filters.timeframe === "custom" ? requestLogTimestamp(filters.until) : undefined;
  const listFilters = useMemo<RequestLogsListFilters>(
    () => ({
      search: filters.search || undefined,
      limit: filters.limit,
      offset: filters.offset,
      accountIds: filters.accountIds,
      apiKeyIds: filters.apiKeyIds,
      statuses: filters.statuses,
      modelOptions: filters.modelOptions,
      timeframe,
      since,
      until,
      conversationId: filters.conversationId ?? undefined,
    }),
    [filters, timeframe, since, until],
  );
  const facetFilters = useMemo<RequestLogFacetFilters>(
    () => ({
      timeframe,
      since,
      until,
      accountIds: filters.accountIds,
      apiKeyIds: filters.apiKeyIds,
      modelOptions: filters.modelOptions,
    }),
    [filters.accountIds, filters.apiKeyIds, filters.modelOptions, timeframe, since, until],
  );

  const {
    data: logsResult,
    error: logsError,
    isFetching: logsIsFetching,
    isLoading: logsIsLoading,
    isPending: logsIsPending,
    isPlaceholderData: logsIsPlaceholderData,
    isSuccess: logsIsSuccess,
    refetch: refetchLogs,
  } = useQuery({
    queryKey: ["dashboard", "request-logs", listFilters],
    queryFn: async ({ signal }) => ({
      page: await getRequestLogs(listFilters, signal),
      filtersApplied,
    }),
    enabled: enabled && rangeValid && searchAdmission.search === filters.search && searchAdmission.settled,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    placeholderData: keepPreviousData,
  });
  const logsData = logsResult?.page;
  const emptyStateFiltersApplied =
    filtersApplied || (logsIsPlaceholderData && Boolean(logsResult?.filtersApplied));
  const logsQuery = {
    data: logsData,
    error: logsError,
    isFetching: logsIsFetching,
    isLoading: logsIsLoading,
    isPending: logsIsPending,
    isPlaceholderData: logsIsPlaceholderData,
    isSuccess: logsIsSuccess,
    refetch: refetchLogs,
  };

  const {
    data: optionsData,
    error: optionsError,
    isFetching: optionsIsFetching,
    isLoading: optionsIsLoading,
    isPending: optionsIsPending,
    isSuccess: optionsIsSuccess,
    refetch: refetchOptions,
  } = useQuery({
    queryKey: ["dashboard", "request-log-options", facetFilters],
    queryFn: ({ signal }) => getRequestLogOptions(facetFilters, signal),
    enabled: enabled && rangeValid,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  });
  const optionsQuery = {
    data: optionsData,
    error: optionsError,
    isFetching: optionsIsFetching,
    isLoading: optionsIsLoading,
    isPending: optionsIsPending,
    isSuccess: optionsIsSuccess,
    refetch: refetchOptions,
  };

  const updateFilters = (patch: Partial<FilterState>) => {
    const nextState: FilterState = {
      ...filters,
      ...patch,
      ...(patch.timeframe && patch.timeframe !== "custom" ? { since: undefined, until: undefined } : {}),
    };
    setSearchParams(writeFilterState(nextState, searchParams));
  };

  return {
    filters,
    rangeValid,
    listFilters,
    facetFilters,
    emptyStateFiltersApplied,
    logsQuery,
    optionsQuery,
    updateFilters,
  };
}
