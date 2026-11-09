import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { PropsWithChildren } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { useApiKeys as useSettingsKeys } from "@/features/api-keys/hooks/use-api-keys";
import { useApiKeys, useApiKeyTrends, useApiKeyUsage7Day } from "@/features/apis/hooks/use-apis";
import { createApiKey, createApiKeyTrends, createApiKeyUsage7Day } from "@/test/mocks/factories";
import { server } from "@/test/mocks/server";
import { renderWithProviders } from "@/test/utils";

afterEach(() => vi.unstubAllGlobals());

function getParentRow(cell: HTMLElement): HTMLElement {
  const row = cell.closest("tr");
  if (!row) throw new Error("Expected element to be inside a table row");
  return row;
}

async function openRowActions(user: ReturnType<typeof userEvent.setup>, row: HTMLElement) {
  const actionsButton = within(row).getByRole("button", { name: "Actions" });
  await user.click(actionsButton);
}

describe("api keys flow integration", () => {
  it("creates, shows plain key dialog, edits, and deletes an api key", async () => {
    const user = userEvent.setup();
    const createdName = "Integration Key";
    const updatedName = "Integration Key Updated";

    window.history.pushState({}, "", "/settings");
    renderWithProviders(<App />);

    const createButton = await screen.findByRole("button", { name: "Create key" });
    expect(createButton).toBeInTheDocument();
    await user.click(createButton);
    await user.type(screen.getByLabelText("Name"), createdName);
    await user.click(screen.getByRole("button", { name: "Create" }));

    const createdDialog = await screen.findByRole("dialog", { name: "API key created" });
    expect(screen.getByText(/sk-test-generated/i)).toBeInTheDocument();
    const closeCandidates = within(createdDialog).getAllByRole("button", {
      name: "Close",
    });
    const closeButton =
      closeCandidates.find((element) => element.getAttribute("data-slot") === "button") ??
      closeCandidates[0];
    await user.click(closeButton);

    const createdRow = getParentRow(await screen.findByText(createdName));

    await openRowActions(user, createdRow);
    await user.click(await screen.findByRole("menuitem", { name: /Edit/ }));
    const nameInput = await screen.findByLabelText("Name");
    await user.clear(nameInput);
    await user.type(nameInput, updatedName);
    await user.click(screen.getByRole("button", { name: "Save" }));

    const updatedRow = getParentRow(await screen.findByText(updatedName));

    await openRowActions(user, updatedRow);
    await user.click(await screen.findByRole("menuitem", { name: /Delete/ }));
    const confirmTitle = await screen.findByText("Delete API key");
    const confirmDialog = confirmTitle.closest("[role='alertdialog']");
    expect(confirmDialog).not.toBeNull();
    if (!confirmDialog) throw new Error("Expected confirm dialog");
    await user.click(
      within(confirmDialog as HTMLElement).getByRole("button", { name: "Delete" }),
    );

    await waitFor(() => {
      expect(screen.queryByText(updatedName)).not.toBeInTheDocument();
    });
  });

  it("creates an api key with assigned accounts", async () => {
    const user = userEvent.setup();

    window.history.pushState({}, "", "/settings");
    renderWithProviders(<App />);

    await user.click(await screen.findByRole("button", { name: "Create key" }));
    await user.type(screen.getByLabelText("Name"), "Scoped Integration Key");
    await user.click(await screen.findByRole("button", { name: "All accounts" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: /primary@example\.com/i }));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Create" }));

    const createdDialog = await screen.findByRole("dialog", { name: "API key created" });
    const closeCandidates = within(createdDialog).getAllByRole("button", {
      name: "Close",
    });
    const closeButton =
      closeCandidates.find((element) => element.getAttribute("data-slot") === "button") ??
      closeCandidates[0];
    await user.click(closeButton);

    const createdRow = getParentRow(await screen.findByText("Scoped Integration Key"));
    await openRowActions(user, createdRow);
    await user.click(await screen.findByRole("menuitem", { name: /Edit/ }));

    expect(await screen.findByRole("button", { name: "1 account selected" })).toBeInTheDocument();
  });

  it.each([
    ["/apis", "/api/api-keys/", ["api-keys", "list"], [createApiKey()]],
    ["/settings", "/api/api-keys/", ["api-keys", "list"], [createApiKey()]],
    ["/apis", "/api/api-keys/key_1/trends", ["api-keys", "trends", "key_1"], createApiKeyTrends()],
    ["/apis", "/api/api-keys/key_1/usage-7d", ["api-keys", "usage-7d", "key_1"], createApiKeyUsage7Day()],
  ] as const)("retries a failed read on %s at %s", async (page, endpoint, queryKey, recovered) => {
    let requests = 0;
    server.use(http.get(endpoint, () => {
      requests += 1;
      return requests === 1
        ? HttpResponse.json({ error: { code: "dashboard_query_timeout", message: "Query timed out" } }, { status: 503 })
        : HttpResponse.json(recovered);
    }));
    window.history.pushState({}, "", page);
    const { queryClient } = renderWithProviders(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(requests).toBe(2));
    await waitFor(() => expect(queryClient.getQueryState(queryKey)?.status).toBe("success"));
    expect(queryClient.getQueryState(queryKey)?.error).toBeNull();
  });

  it("aborts list and detail reads when their last observers leave", async () => {
    const requests: { url: string; signal: AbortSignal }[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string, options: RequestInit) => {
      const signal = options.signal;
      if (!signal) throw new Error("Missing query abort signal");
      requests.push({ url, signal });
      return new Promise<Response>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    function Wrapper({ children }: PropsWithChildren) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    const page = renderHook(() => {
      useApiKeys();
      useApiKeyTrends("key_1");
      useApiKeyUsage7Day("key_1");
    }, { wrapper: Wrapper });
    const settings = renderHook(() => useSettingsKeys(), { wrapper: Wrapper });
    try {
      await waitFor(() => expect(requests).toHaveLength(3));
      page.unmount();
      expect(requests.find(({ url }) => url.endsWith("/"))?.signal.aborted).toBe(false);
      expect(requests.filter(({ url }) => !url.endsWith("/")).every(({ signal }) => signal.aborted)).toBe(true);
      settings.unmount();
      expect(requests.every(({ signal }) => signal.aborted)).toBe(true);
    } finally {
      page.unmount();
      settings.unmount();
      client.clear();
    }
  });
});
