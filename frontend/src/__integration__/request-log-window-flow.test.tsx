import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { expect, it } from "vitest";

import App from "@/App";
import { createDashboardOverview } from "@/test/mocks/factories";
import { server } from "@/test/mocks/server";
import { renderWithProviders } from "@/test/utils";

it("keeps custom range controls usable before a first log response and resets to bounded browsing", async () => {
  const requests: URL[] = [];
  let overviewCalls = 0;
  server.use(
    http.get("/api/dashboard/overview", () => {
      overviewCalls += 1;
      return HttpResponse.json(createDashboardOverview());
    }),
    http.get("/api/request-logs", ({ request }) => {
      requests.push(new URL(request.url));
      return HttpResponse.json({ requests: [], total: 0, hasMore: false });
    }),
    http.get("/api/request-logs/options", ({ request }) => {
      requests.push(new URL(request.url));
      return HttpResponse.json({ accountIds: [], apiKeys: [], modelOptions: [], statuses: [] });
    }),
  );
  window.history.replaceState({}, "", "/dashboard?timeframe=custom&since=2020-01-01T00:00:00Z&until=2020-01-09T00:00:00Z");
  renderWithProviders(<App />);
  const end = await screen.findByLabelText("End time (UTC)");
  expect(requests).toHaveLength(0);
  const initialOverviewCalls = overviewCalls;
  fireEvent.change(end, { target: { value: "2020-01-08T00:00:00" } });
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests.every((request) => request.searchParams.get("since") === "2020-01-01T00:00:00.000Z")).toBe(true);
  expect(requests.every((request) => request.searchParams.get("until") === "2020-01-08T00:00:00.000Z")).toBe(true);
  await userEvent.setup().click(screen.getByRole("button", { name: "Reset" }));
  await waitFor(() => expect(requests).toHaveLength(4));
  expect(requests.slice(2).every((request) => request.searchParams.get("timeframe") === "24h")).toBe(true);
  expect(new URLSearchParams(window.location.search).has("since")).toBe(false);
  expect(new URLSearchParams(window.location.search).has("until")).toBe(false);
  expect(overviewCalls).toBe(initialOverviewCalls);
});
