import { useQuery } from "@tanstack/react-query";
import type { ChangeEvent } from "react";
import { api } from "../lib/api";

/** The Lots page's filters, named like the API's query parameters; "" or absent = any. */
export type FilterKey =
  | "make" | "model" | "year_min" | "year_max" | "damage" | "location_state" | "yard"
  | "sale_from" | "sale_to" | "keys" | "runs" | "odometer_max";
export type Filters = Partial<Record<FilterKey, string>>;

export const activeCount = (filters: Filters) => Object.values(filters).filter((v) => v?.trim()).length;

/** Makes and yards are too many for a list: a text box suggests them (datalist). */
export function LotFilters({ value, onChange }: { value: Filters; onChange: (filters: Filters) => void }) {
  const options = useQuery({ queryKey: ["lot-filters"], queryFn: api.lotFilters, staleTime: 15 * 60_000 });
  const field = (key: FilterKey) => ({
    value: value[key] ?? "",
    onChange: (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => onChange({ ...value, [key]: e.target.value }),
  });
  const pick = (key: FilterKey, label: string, values: string[] = []) => (
    <label>
      {label}
      <select {...field(key)}>
        <option value="">Any</option>
        {values.map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    </label>
  );
  const o = options.data;

  return (
    <div className="filter-panel" role="group" aria-label="Lot filters">
      <label>Make<input type="text" list="filter-makes" placeholder="Any" {...field("make")} /></label>
      <datalist id="filter-makes">{o?.makes.map((m) => <option key={m} value={m} />)}</datalist>
      <label>Model<input type="text" placeholder="Starts with…" {...field("model")} /></label>
      <label>Year from<input type="number" inputMode="numeric" placeholder="Any" {...field("year_min")} /></label>
      <label>Year to<input type="number" inputMode="numeric" placeholder="Any" {...field("year_max")} /></label>
      {pick("damage", "Primary damage", o?.damages)}
      {pick("location_state", "State", o?.states)}
      <label>Yard<input type="text" list="filter-yards" placeholder="Any" {...field("yard")} /></label>
      <datalist id="filter-yards">{o?.yards.map((y) => <option key={y} value={y} />)}</datalist>
      <label>Sale from<input type="date" {...field("sale_from")} /></label>
      <label>Sale to<input type="date" {...field("sale_to")} /></label>
      {pick("keys", "Keys", o?.keys)}
      {pick("runs", "Runs / drives", o?.runs)}
      <label>Max odometer<input type="number" inputMode="numeric" min={0} step={1000} placeholder="Any" {...field("odometer_max")} /></label>
      {options.error && <div className="error small">Filter values unavailable: {options.error.message}</div>}
    </div>
  );
}
