import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import { api, ApiError, type AnalysisResult, type LotAnalyses, type VehicleAnalysis } from "../lib/api";
import { AnalysisButton, AnalysisSection, words } from "./Analysis";

afterEach(() => vi.restoreAllMocks());

const result: AnalysisResult = {
  schema_version: "1.0", language: "en",
  general_summary: {
    summary: "2013 Acura TSX with a heavy front impact.",
    key_findings: ["Front bumper torn off"], inspection_priorities: ["Radiator support"],
  },
  damage_analysis: {
    summary: "Front-end collision centred on the bumper.",
    observed_damage: [{
      damage_id: "damage_01", part_name: "front_bumper", location: "front", severity: "severe",
      description: "Bumper cover torn and pushed in.", confidence: 0.94,
      evidence_image_ids: ["image_01", "image_02"],
    }],
    possible_hidden_damage: [{
      damage_id: "possible_damage_01", part_name: "radiator", description: "Radiator may be cracked.",
      reason: "It sits right behind the crushed bumper.", based_on_observed_damage_ids: ["damage_01"],
      confidence: 0.62, inspection_recommended: true,
    }],
  },
  damaged_parts: [{
    part_name: "front_bumper", severity: "severe", suggested_action: "repair_or_replace",
    confidence: 0.9, observed_damage_ids: ["damage_01"],
  }],
  image_annotations: [{
    image_id: "image_01",
    regions: [{
      region_id: "region_01", damage_id: "damage_01", part_name: "front_bumper",
      label: "Front bumper damage", explanation: "The cover is torn at the left corner.",
      confidence: 0.9, bounding_box: { x: 0.1, y: 0.5, width: 0.6, height: 0.4 },
    }],
  }],
  limitations: ["No photo of the underside."],
};

const completed: VehicleAnalysis = {
  id: 1, source: "copart", lot_number: "68264956", status: "completed", language: "en",
  requested_by: "admin", provider: "anthropic", model: "claude-opus-5-5",
  prompt_version: "vehicle-damage-v1", schema_version: "1.0",
  images: [
    { image_id: "image_01", position: 3, thumb: null, full: "https://cs.copart.com/3_ful.jpg", hd: null },
    { image_id: "image_02", position: 6, thumb: null, full: null, hd: "https://cs.copart.com/6_hrs.jpg" },
    { image_id: "image_03", position: 7, thumb: "https://cs.copart.com/7_thb.jpg", full: null, hd: null },
  ],
  image_count: 3, result, error: null, created_at: "2026-09-28T10:00:00Z",
  started_at: "2026-09-28T10:00:02Z", completed_at: "2026-09-28T10:01:30Z", updated_at: "2026-09-28T10:01:30Z",
};

const attempt = (status: VehicleAnalysis["status"], extra: Partial<VehicleAnalysis> = {}): VehicleAnalysis => ({
  ...completed, id: 2, status, images: null, image_count: null, result: null, completed_at: null, ...extra,
});

function show(data: LotAnalyses | Error) {
  const load = vi.spyOn(api, "lotAnalyses");
  if (data instanceof Error) load.mockRejectedValue(data);
  else load.mockResolvedValue(data);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AnalysisButton source="copart" lotNumber="68264956" />
      <AnalysisSection source="copart" lotNumber="68264956" />
    </QueryClientProvider>,
  );
  return load;
}

describe("AI analysis", () => {
  it("generates the first analysis and shows it is running", async () => {
    const load = show({ latest: null, current: null });
    const request = vi.spyOn(api, "requestAnalysis").mockResolvedValue(attempt("queued"));
    expect(await screen.findByText(/No analysis yet/)).toBeInTheDocument();

    load.mockResolvedValue({ latest: null, current: attempt("queued") });
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    expect(await screen.findByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
    expect(request).toHaveBeenCalledWith("copart", "68264956");
    expect(await screen.findByRole("status")).toHaveTextContent("queued");
    expect(screen.queryByText(/No analysis yet/)).not.toBeInTheDocument();
  });

  it("shows every section of the latest analysis and offers to regenerate it", async () => {
    show({ latest: completed, current: completed });
    expect(await screen.findByRole("button", { name: "Regenerate AI Analysis" })).toBeEnabled();
    for (const name of ["General Summary", "Damage Analysis", "Observed Damage", "Possible Hidden Damage",
      "Damaged Parts", "Interactive Damage Images", "Limitations"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.getByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();
    expect(screen.getByText("Radiator support")).toBeInTheDocument();
    expect(screen.getByText("Front-end collision centred on the bumper.")).toBeInTheDocument();
    expect(screen.getByText("#3, #6")).toBeInTheDocument();  // listing positions of the evidence
    expect(screen.getByText("It sits right behind the crushed bumper.")).toBeInTheDocument();
    expect(screen.getAllByText("Front bumper")).toHaveLength(3);  // observed, parts, hidden's basis
    expect(screen.getByText("Repair or replace")).toBeInTheDocument();
    expect(screen.getByText("No photo of the underside.")).toBeInTheDocument();
    expect(screen.getByText(/claude-opus-5-5 · prompt vehicle-damage-v1 · schema 1.0 · 3 photos/)).toBeInTheDocument();
    expect(screen.getByText(/requested by admin/)).toBeInTheDocument();
    expect(screen.getByText("Raw analysis JSON").closest("details")).toHaveTextContent('"schema_version": "1.0"');
  });

  it("marks damage on the original photos and explains a marked area on hover or click", async () => {
    show({ latest: completed, current: completed });
    const box = await screen.findByRole("button", { name: "Front bumper damage, 90% confidence" });
    expect(box).toHaveStyle({ left: "10%", top: "50%", width: "60%", height: "40%" });
    const photos = screen.getAllByRole("figure");
    expect(within(photos[0]).getByRole("img")).toHaveAttribute("src", "https://cs.copart.com/3_ful.jpg");
    expect(within(photos[1]).getByRole("img")).toHaveAttribute("src", "https://cs.copart.com/6_hrs.jpg");
    expect(within(photos[2]).getByRole("img")).toHaveAttribute("src", "https://cs.copart.com/7_thb.jpg");
    expect(within(photos[1]).getByText("No damage marked on this photo.")).toBeInTheDocument();
    expect(within(photos[0]).getByText("Point at or select a marked area for details.")).toBeInTheDocument();

    fireEvent.mouseEnter(box);
    expect(within(photos[0]).getByText("The cover is torn at the left corner.")).toBeInTheDocument();
    expect(within(photos[0]).getByText("90% confidence")).toBeInTheDocument();
    expect(box).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(box);
    fireEvent.focus(box);
    expect(within(photos[0]).getByText("severe")).toBeInTheDocument();
  });

  it("keeps the previous analysis when a regeneration fails, and says so", async () => {
    const load = show({ latest: completed, current: attempt("failed", {
      error: "AIError: the AI provider rejected the request (529): overloaded",
      completed_at: "2026-09-28T11:00:00Z" }) });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /The latest analysis failed .*: AIError: the AI provider rejected the request \(529\): overloaded The previous analysis is shown\./);
    expect(screen.getByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();

    vi.spyOn(api, "requestAnalysis").mockResolvedValue(attempt("queued", { id: 3 }));
    load.mockResolvedValue({ latest: completed, current: attempt("queued", { id: 3 }) });
    fireEvent.click(screen.getByRole("button", { name: "Regenerate AI Analysis" }));
    expect(await screen.findByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
    expect(await screen.findByRole("status")).toHaveTextContent("The previous analysis is shown until then.");
    expect(screen.getByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();
  });

  it("reports a failed first analysis", async () => {
    show({ latest: null, current: attempt("failed", { error: "AnalysisError: the lot has no usable photos" }) });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("AnalysisError: the lot has no usable photos");
    expect(alert).not.toHaveTextContent("previous");
    expect(screen.getByText(/No analysis yet/)).toBeInTheDocument();
  });

  it("shows the previous analysis while a new one is generated", async () => {
    show({ latest: completed, current: attempt("processing") });
    expect(await screen.findByRole("status")).toHaveTextContent(
      "generatingAnalyzing the photos; this takes a minute or two. The previous analysis is shown until then.");
    expect(screen.getByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
  });

  it("says what an analysis found nothing of", async () => {
    const empty: AnalysisResult = {
      ...result, general_summary: { ...result.general_summary, key_findings: [], inspection_priorities: [] },
      damage_analysis: { summary: "No damage visible.", observed_damage: [], possible_hidden_damage: [] },
      damaged_parts: [], image_annotations: [], limitations: [],
    };
    show({ latest: { ...completed, result: empty, requested_by: null }, current: null });
    expect(await screen.findByText("No damage visible in the photos.")).toBeInTheDocument();
    expect(screen.getByText("None suspected.")).toBeInTheDocument();
    expect(screen.getAllByText("None.")).toHaveLength(3);
    expect(screen.getByText("None stated.")).toBeInTheDocument();
    expect(screen.queryByText(/requested by/)).not.toBeInTheDocument();
  });

  it("shows why a request was refused", async () => {
    show({ latest: null, current: null });
    vi.spyOn(api, "requestAnalysis").mockRejectedValue(
      new ApiError(409, "an analysis of this lot is already in progress"));
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("an analysis of this lot is already in progress");
  });

  it("shows a load error and keeps the button disabled", async () => {
    show(new Error("lot not found"));
    expect(await screen.findByText("lot not found")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate AI Analysis" })).toBeDisabled());
  });
});

describe("words", () => {
  it("turns snake_case names into words", () => {
    expect(words("left_front_fender")).toBe("Left front fender");
    expect(words("")).toBe("");
  });
});
