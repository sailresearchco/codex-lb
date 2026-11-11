import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import type { ReportSpeedResponse } from "../schemas";

export function SpeedCoverage({ data, fetching, retry }: {
  data: ReportSpeedResponse;
  fetching: boolean;
  retry: () => void;
}) {
  const { t } = useTranslation();
  const metrics = [
    ["ttft", t("reports.charts.medianTtft")],
    ["tps", t("reports.charts.medianTps")],
    ["queue", t("reports.charts.medianQueueWait")],
  ] as const;
  const format = (value: number | null, fallback: string) =>
    value === null ? fallback : String(value);

  return <div className="space-y-2 text-sm text-muted-foreground">
    {!data.available ? (
      <div className="flex items-center justify-between gap-2" role="status">
        <p>{data.unavailableReason === "building"
          ? t("reports.speedBuilding", { defaultValue: "Speed history is still being summarized. Cost and token totals remain available." })
          : data.unavailableReason === "raw_history_pruned"
            ? t("reports.speedHistoryPruned", { defaultValue: "Complete speed history is unavailable for this range. Choose a range within the available coverage." })
            : t("reports.speedUnavailable", { days: data.maxDays })}</p>
        {data.unavailableReason === "building" ? (
          <Button variant="outline" size="sm" disabled={fetching} onClick={retry}>
            {t("common.actions.retry")}
          </Button>
        ) : null}
      </div>
    ) : data.approximate ? (
      <p>{t("reports.speedApproximate", {
        defaultValue: "Speed charts show approximate medians. Sample counts and bounds below show the precision of each estimate.",
      })}{data.generatedAt ? ` ${t("reports.asOf", { time: new Date(data.generatedAt).toLocaleString() })}` : null}</p>
    ) : null}
    {data.coverageFrom && data.foldedThrough ? (
      <p className="text-xs">{t("reports.speedCoverage", {
        defaultValue: "Summarized coverage: {{start}} to {{end}}.",
        start: new Date(data.coverageFrom).toLocaleString(),
        end: new Date(data.foldedThrough).toLocaleString(),
      })}{data.available ? ` ${t("reports.speedLiveTail", { defaultValue: "Recent requests are included separately." })}` : null}</p>
    ) : null}
    {data.available && data.approximate ? (
      <details>
        <summary className="cursor-pointer">{t("reports.speedPrecision", { defaultValue: "Speed estimate bounds" })}</summary>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead><tr>
              <th>{t("reports.date", { defaultValue: "Date" })}</th>
              <th>{t("reports.metric", { defaultValue: "Metric" })}</th>
              <th>{t("reports.samples", { defaultValue: "Samples" })}</th>
              <th>{t("reports.medianBounds", { defaultValue: "Median bounds" })}</th>
              <th>{t("reports.maxRelativeError", { defaultValue: "Maximum relative error" })}</th>
            </tr></thead>
            <tbody>{data.daily.flatMap((day) => metrics.map(([metric, label]) => {
              const estimate = day[metric];
              if (!estimate) return null;
              const unit = metric === "tps" ? "tokens/s" : "ms";
              return <tr key={`${day.date}-${metric}`}>
                <td>{day.date}</td><td>{label}</td><td>{estimate.sampleCount.toLocaleString()}</td>
                <td>{estimate.sampleCount ? `${format(estimate.lowerBound, "−∞")} – ${format(estimate.upperBound, "∞")} ${unit}` : "—"}</td>
                <td>{estimate.relativeErrorBound === null ? "—" : `≤ ${(estimate.relativeErrorBound * 100).toFixed(2)}%`}</td>
              </tr>;
            }))}</tbody>
          </table>
        </div>
      </details>
    ) : null}
  </div>;
}
