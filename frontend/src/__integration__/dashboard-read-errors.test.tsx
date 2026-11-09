import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { AccountsPage } from "@/features/accounts/components/accounts-page";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { DashboardPage } from "@/features/dashboard/components/dashboard-page";
import {
  ADMIN_PERMISSIONS,
  createAccountTrends,
  createDashboardOverview,
  createDashboardProjections,
  createDefaultAccounts,
} from "@/test/mocks/factories";
import { server } from "@/test/mocks/server";
import { renderWithProviders } from "@/test/utils";

beforeEach(() => {
  useAuthStore.setState({ initialized: true, role: "admin", permissions: ADMIN_PERMISSIONS, canWrite: true });
});

describe("dashboard read recovery", () => {
  it.each([
    {
      name: "accounts",
      path: "/api/accounts",
      queryKey: ["accounts", "list"],
      Page: AccountsPage,
      response: () => ({ accounts: createDefaultAccounts() }),
    },
    {
      name: "account trends",
      path: "/api/accounts/:accountId/trends",
      queryKey: ["accounts", "trends"],
      Page: AccountsPage,
      response: () => createAccountTrends("acc_primary"),
    },
    {
      name: "projections",
      path: "/api/dashboard/projections",
      queryKey: ["dashboard", "projections"],
      Page: DashboardPage,
      response: () => createDashboardProjections(),
    },
  ])("retries only the failed $name read from the keyboard", async ({ path, queryKey, Page, response }) => {
    const user = userEvent.setup({ delay: null });
    let available = false;
    let requests = 0;
    let overviewRequests = 0;
    let release = () => {};
    const recovery = new Promise<void>((resolve) => { release = resolve; });
    server.use(
      http.get(path, async () => {
        requests += 1;
        if (!available) {
          return HttpResponse.json({ error: { code: "read_failed", message: "Read failed" } }, { status: 503 });
        }
        await recovery;
        return HttpResponse.json(response());
      }),
      http.get("/api/dashboard/overview", () => {
        overviewRequests += 1;
        return HttpResponse.json(createDashboardOverview());
      }),
    );
    const { queryClient, unmount } = renderWithProviders(<Page />);
    try {
      await waitFor(() => {
        expect(queryClient.getQueryCache().find({ queryKey, exact: false })?.state.status).toBe("error");
      });
      const alert = await screen.findByRole("alert");
      const retry = within(alert.parentElement!).getByRole("button");
      const beforeRetry = requests;
      const beforeOverview = overviewRequests;
      available = true;
      retry.focus();
      await user.keyboard("{Enter}");
      await waitFor(() => expect(requests).toBe(beforeRetry + 1));
      expect(!retry.isConnected || retry.hasAttribute("disabled")).toBe(true);
      expect(overviewRequests).toBe(beforeOverview);
      release();
      await waitFor(() => {
        expect(queryClient.getQueryCache().find({ queryKey, exact: false })?.state.status).toBe("success");
      });
      expect(overviewRequests).toBe(beforeOverview);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      release();
      unmount();
    }
  });
});
