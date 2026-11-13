import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ResetCreditSettings } from "@/features/settings/components/reset-credit-settings";
import { buildSettingsUpdateRequest } from "@/features/settings/payload";
import type { DashboardSettings, SettingsUpdateRequest } from "@/features/settings/schemas";
import { createDashboardSettings } from "@/test/mocks/factories";

describe("ResetCreditSettings", () => {
  it("renders the reset-credit switches with current settings", () => {
    render(
      <ResetCreditSettings
        settings={createDashboardSettings({
          showResetCreditBadges: true,
          autoRedeemResetCreditsBeforeExpiry: false,
          showResetCreditExpiryBadge: true,
        })}
        busy={false}
        onSave={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByRole("switch", { name: "Show reset-credit badges" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Automatically use reset credits" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Show reset action expiry" })).toBeChecked();
  });

  it.each([
    {
      name: "Show reset-credit badges",
      settings: createDashboardSettings({ showResetCreditBadges: true }),
      patch: { showResetCreditBadges: false },
    },
    {
      name: "Automatically use reset credits",
      settings: createDashboardSettings({ autoRedeemResetCreditsBeforeExpiry: false }),
      patch: { autoRedeemResetCreditsBeforeExpiry: true },
    },
    {
      name: "Show reset action expiry",
      settings: createDashboardSettings({ showResetCreditExpiryBadge: true }),
      patch: { showResetCreditExpiryBadge: false },
    },
  ] satisfies Array<{
    name: string;
    settings: DashboardSettings;
    patch: Partial<SettingsUpdateRequest>;
  }>)("saves $name changes through the settings payload", async ({ name, settings, patch }) => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);

    render(<ResetCreditSettings settings={settings} busy={false} onSave={onSave} />);

    await user.click(screen.getByRole("switch", { name }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith(buildSettingsUpdateRequest(settings, patch));
  });

  it("disables reset-credit switches while settings are busy", () => {
    render(
      <ResetCreditSettings
        settings={createDashboardSettings()}
        busy={true}
        onSave={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByRole("switch", { name: "Show reset-credit badges" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Automatically use reset credits" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Show reset action expiry" })).toBeDisabled();
  });
});
