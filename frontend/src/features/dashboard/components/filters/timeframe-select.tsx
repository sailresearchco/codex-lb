import { useTranslation } from "react-i18next";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const TIMEFRAME_VALUES = ["1h", "24h", "7d", "custom"] as const;
export type TimeframeValue = (typeof TIMEFRAME_VALUES)[number];

function isTimeframeValue(value: string): value is TimeframeValue {
  return (TIMEFRAME_VALUES as readonly string[]).includes(value);
}

export type TimeframeSelectProps = {
  value: TimeframeValue;
  onChange: (value: TimeframeValue) => void;
};

export function TimeframeSelect({ value, onChange }: TimeframeSelectProps) {
  const { t } = useTranslation();

  return (
    <Select value={value} onValueChange={(next) => { if (isTimeframeValue(next)) onChange(next); }}>
      <SelectTrigger size="sm" className="w-28">
        <SelectValue placeholder={t("dashboard.filters.timeframe")} />
      </SelectTrigger>
      <SelectContent align="start">
        <SelectItem value="1h">1h</SelectItem>
        <SelectItem value="24h">24h</SelectItem>
        <SelectItem value="7d">7d</SelectItem>
        <SelectItem value="custom">{t("dashboard.filters.customRange")}</SelectItem>
      </SelectContent>
    </Select>
  );
}
