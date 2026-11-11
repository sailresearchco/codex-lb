import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/utils";
import { SpeedCoverage } from "./speed-coverage";

describe("speed coverage recovery", () => {
  it("retries building coverage and prevents concurrent retries", async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const data = { available: false, maxDays: 7, unavailableReason: "building" as const, daily: [] };
    const view = renderWithProviders(<SpeedCoverage data={data} fetching={false} retry={retry} />);
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(retry).toHaveBeenCalledOnce();
    view.rerender(<SpeedCoverage data={data} fetching retry={retry} />);
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
