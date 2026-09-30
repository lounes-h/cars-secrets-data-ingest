import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import { api, ApiError, type AnalysisResult, type SentRequest, type VehicleAnalysis } from "../lib/api";
import { dateTime } from "../lib/format";
import { AnalysisSection, tally, words } from "./Analysis";

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

const sent: SentRequest = {
  model: "claude-opus-5-5", max_tokens: 32000, system: "You are a senior vehicle damage appraiser.",
  output_config: { effort: "high", format: { type: "json_schema", schema: { type: "object", additionalProperties: false } } },
  betas: ["server-side-fallback-2026-07-01"], fallbacks: "default", timeout_seconds: 300,
  messages: [{
    role: "user", content: [
      { type: "text", text: 'Auction metadata (reported by the auction or the seller; may be incomplete or wrong):\n{"make": "ACURA"}' },
      { type: "text", text: "image_01" }, { type: "image", media_type: "image/jpeg", bytes: 120832 },
      { type: "image", media_type: "image/jpeg", bytes: 2048 },
    ],
  }],
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
  request: {
    settings: { provider: "anthropic", model: "claude-opus-5-5", prompt_version: "vehicle-damage-v1", image_max_px: 1024 },
    selection: { ...sent, system: "You choose which auction photos an appraiser should study." },
    analysis: sent,
  },
  usage: {
    selection: { model: "claude-opus-5-5", input_tokens: 2400, output_tokens: 600, cost_usd: 0.0216 },
    analysis: { model: "claude-opus-5-5", input_tokens: 9000, output_tokens: 3500, cost_usd: 0.106 },
  },
  started_at: "2026-09-28T10:00:02Z", completed_at: "2026-09-28T10:01:30Z", updated_at: "2026-09-28T10:01:30Z",
};

const attempt = (status: VehicleAnalysis["status"], extra: Partial<VehicleAnalysis> = {}): VehicleAnalysis => ({
  ...completed, id: 2, status, images: null, image_count: null, result: null, usage: null,
  completed_at: null, ...extra,
});

const models = {
  default: "claude-opus-5-5",
  models: [
    { id: "claude-opus-5-5", label: "Claude Opus 5.5", note: "Anthropic's current Opus model", input_usd: 4, output_usd: 20, provider: "anthropic", custom: false },
    { id: "claude-sonnet-5-5", label: "Claude Sonnet 5.5", note: "Faster, half the price of Opus 5.5", input_usd: 2, output_usd: 10, provider: "anthropic", custom: false },
  ],
};
const gpt = { id: "gpt-5.6", label: "GPT-5.6", note: "", input_usd: 2, output_usd: 8, provider: "openai", custom: true };

/** Opens the model dialog with the page's button and confirms with the chosen model. */
async function generateWith(button: string, model?: string) {
  fireEvent.click(await screen.findByRole("button", { name: button }));
  const dialog = screen.getByRole("dialog", { name: button });
  if (model) fireEvent.click(within(dialog).getByRole("radio", { name: new RegExp(model) }));
  fireEvent.click(within(dialog).getByRole("button", { name: "Generate" }));
  return dialog;
}

/** Renders the section over a lot with these analyses (newest first). The list is served as the
 * API lists it, without photos, result or request; each analysis in full by its id. Returns a
 * function that changes the analyses, and the query client, to refresh as a live event does. */
function show(analyses: VehicleAnalysis[] | Error, offered: typeof models | Error = models) {
  const list = vi.spyOn(api, "analysisModels");
  if (offered instanceof Error) list.mockRejectedValue(offered);
  else list.mockResolvedValue(offered);
  const known = new Map<number, VehicleAnalysis>();
  const load = vi.spyOn(api, "lotAnalyses");
  const listed = (next: VehicleAnalysis[]) => {
    next.forEach((a) => known.set(a.id, a));
    load.mockResolvedValue({ analyses: next.map(({ images: _i, result: _r, request: _q, ...summary }) => summary) });
  };
  if (analyses instanceof Error) load.mockRejectedValue(analyses);
  else listed(analyses);
  vi.spyOn(api, "lotAnalysis").mockImplementation(async (_source, _lot, id) => known.get(id)!);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <AnalysisSection source="copart" lotNumber="68264956" />
    </QueryClientProvider>,
  );
  return { listed, qc };
}

/** The value of one of the shown analysis's facts. */
const fact = (label: string) => screen.getByText(label, { selector: "dt" }).nextElementSibling!;

/** The history's rows, newest first. */
const historyRows = () => within(screen.getByRole("table", { name: "Analysis history" })).getAllByRole("row").slice(1);

describe("AI analysis", () => {
  it("generates the first analysis and shows it is running", async () => {
    const { listed } = show([]);
    const request = vi.spyOn(api, "requestAnalysis").mockResolvedValue(attempt("queued"));
    expect(await screen.findByText(/No analysis yet/)).toBeInTheDocument();

    listed([attempt("queued")]);
    const dialog = await generateWith("Generate AI Analysis", "Claude Sonnet 5.5");
    expect(dialog).not.toHaveAttribute("open");
    expect(await screen.findByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
    expect(request).toHaveBeenCalledWith("copart", "68264956", "claude-sonnet-5-5");
    expect(await screen.findByRole("status")).toHaveTextContent(
      "queuedAnalyzing the photos with claude-opus-5-5; this takes a minute or two.");
    expect(screen.queryByText(/No analysis yet/)).not.toBeInTheDocument();
    expect(fact("Cost")).toHaveTextContent("—");
    expect(screen.queryByRole("table", { name: "Analysis history" })).not.toBeInTheDocument();  // just one
  });

  it("shows every section of the latest analysis and offers to regenerate it", async () => {
    show([completed]);
    expect(await screen.findByRole("button", { name: "Regenerate AI Analysis" })).toBeEnabled();
    await screen.findByRole("heading", { name: "General Summary" });  // loaded after the history
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
    expect(screen.getByText("Raw analysis JSON").closest("details")).toHaveTextContent('"schema_version": "1.0"');
  });

  it("heads the analysis with its model, who asked for it, and its cost, tokens and time", async () => {
    show([completed]);
    const head = (await screen.findByText("Cost", { selector: "dt" })).closest<HTMLElement>(".analysis-head")!;
    expect(within(head).getByText("claude-opus-5-5")).toBeInTheDocument();
    expect(head).toHaveTextContent(/Anthropic.*completed/);
    expect(head).toHaveTextContent(`${dateTime(completed.completed_at)} · requested by admin`);
    expect(fact("Cost")).toHaveTextContent("$0.128photo selection $0.022 · analysis $0.106");
    expect(fact("Tokens")).toHaveTextContent("15,50011,400 in · 4,100 out");
    expect(fact("Photos")).toHaveTextContent("3");
    expect(fact("Time")).toHaveTextContent("1m 28s");
    expect(fact("Prompt")).toHaveTextContent("vehicle-damage-v1");
    expect(fact("Schema")).toHaveTextContent("1.0");
  });

  it("shows the settings, prompts and request parameters each model call was sent", async () => {
    show([completed]);
    expect(await screen.findByRole("heading", { name: "Prompt and Request" })).toBeInTheDocument();
    expect(screen.getByText("image_max_px").nextSibling).toHaveTextContent("1024");
    expect(screen.getByText("Photo selection call · claude-opus-5-5 · 2 photos")).toBeInTheDocument();
    const call = screen.getByText("Analysis call · claude-opus-5-5 · 2 photos").closest("details")!;
    expect(within(call).getByText("You are a senior vehicle damage appraiser.")).toBeInTheDocument();
    expect(within(call).getByText(/Auction metadata .* \{"make": "ACURA"\}/)).toBeInTheDocument();
    expect(within(call).getByText("image_01: JPEG photo, 118.0 KB")).toBeInTheDocument();
    expect(within(call).getByText("JPEG photo, 2.0 KB")).toBeInTheDocument();
    const params = within(call).getByText("Parameters").nextElementSibling!;
    expect(params).toHaveTextContent('"effort": "high"');
    expect(params).toHaveTextContent('"format": "json_schema"');
    expect(params).toHaveTextContent('"max_tokens": 32000');
    expect(params).not.toHaveTextContent("senior vehicle damage appraiser");
    expect(within(call).getByText("Output schema (json_schema)").closest("details"))
      .toHaveTextContent('"additionalProperties": false');
  });

  it("shows an OpenAI call as its client recorded it (instructions, input, text format)", async () => {
    const openai: SentRequest = {
      model: "gpt-6.1-sol", max_output_tokens: 32000, instructions: "You are a senior vehicle damage appraiser.",
      reasoning: { effort: "high" }, timeout_seconds: 300,
      text: { format: { type: "json_schema", name: "AnalysisV1", strict: true, schema: { type: "object", additionalProperties: false } } },
      input: [{
        role: "user", content: [
          { type: "input_text", text: "Auction metadata:\n{\"make\": \"ACURA\"}" },
          { type: "input_text", text: "image_01" }, { type: "input_image", media_type: "image/jpeg", bytes: 120832, detail: "high" },
        ],
      }],
    };
    const ran = { ...completed, provider: "openai", model: "gpt-6.1-sol",
      request: { settings: { provider: "openai", model: "gpt-6.1-sol" }, analysis: openai } };
    show([ran]);
    const call = (await screen.findByText("Analysis call · gpt-6.1-sol · 1 photos")).closest("details")!;
    expect(within(call).getByText("You are a senior vehicle damage appraiser.")).toBeInTheDocument();
    expect(within(call).getByText(/Auction metadata: \{"make": "ACURA"\}/)).toBeInTheDocument();
    expect(within(call).getByText("image_01: JPEG photo, 118.0 KB")).toBeInTheDocument();
    const params = within(call).getByText("Parameters").nextElementSibling!;
    expect(params).toHaveTextContent('"effort": "high"');
    expect(params).toHaveTextContent('"format": "json_schema"');
    expect(params).toHaveTextContent('"max_output_tokens": 32000');
    expect(params).not.toHaveTextContent("senior vehicle damage appraiser");
    expect(params).not.toHaveTextContent("additionalProperties");
    expect(within(call).getByText("Output schema (json_schema)").closest("details"))
      .toHaveTextContent('"additionalProperties": false');
  });

  it("marks damage on the original photos and explains a marked area on hover or click", async () => {
    show([completed]);
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

  it("keeps the previous analysis when a regeneration fails, and shows the failure on selection", async () => {
    const failed = attempt("failed", {
      error: "AIError: the answer is not valid JSON", created_at: "2026-09-28T10:59:00Z",
      started_at: "2026-09-28T10:59:10Z", completed_at: "2026-09-28T11:00:00Z",
      usage: { analysis: { model: "claude-opus-5-5", input_tokens: 800, output_tokens: 200, cost_usd: 0.0072 } },
    });
    const { listed, qc } = show([failed, completed]);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /The latest analysis failed .*: AIError: the answer is not valid JSON The previous analysis is shown/);
    expect(await screen.findByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();
    const [newest, older] = historyRows();
    expect(older).toHaveAttribute("aria-current", "true");
    expect(newest).not.toHaveAttribute("aria-current");
    expect(newest).toHaveTextContent(/failed.*1,000.*\$0\.007.*50\.0s/);
    expect(older).toHaveTextContent(/admin.*claude-opus-5-5 · Anthropic.*completed.*3.*15,500.*\$0\.128.*1m 28s/);

    // the failed one shows its error and what it sent, in place of the previous analysis
    fireEvent.click(within(newest).getByRole("button", { name: dateTime(failed.created_at) }));
    expect(newest).toHaveAttribute("aria-current", "true");
    expect(await screen.findByRole("heading", { name: "Prompt and Request" })).toBeInTheDocument();
    expect(screen.getByText("Analysis call · claude-opus-5-5 · 2 photos")).toBeInTheDocument();
    expect(screen.getByText("AIError: the answer is not valid JSON").closest(".banner")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();  // it is the one shown
    expect(screen.queryByText("2013 Acura TSX with a heavy front impact.")).not.toBeInTheDocument();
    fireEvent.click(older);  // anywhere on the row
    expect(await screen.findByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();
    fireEvent.click(newest);

    // regenerating goes back to the latest completed analysis until the new one is done
    const queued = attempt("queued", { id: 3, model: "claude-sonnet-5-5", created_at: "2026-09-28T12:00:00Z" });
    vi.spyOn(api, "requestAnalysis").mockResolvedValue(queued);
    listed([queued, failed, completed]);
    await generateWith("Regenerate AI Analysis", "Claude Sonnet 5.5");
    expect(api.requestAnalysis).toHaveBeenCalledWith("copart", "68264956", "claude-sonnet-5-5");
    expect(await screen.findByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Analyzing the photos with claude-sonnet-5-5; this takes a minute or two. The previous analysis is shown until then.");
    expect(await screen.findByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();
    expect(historyRows()).toHaveLength(3);
    expect(historyRows()[0]).toHaveTextContent("queued");

    const done = { ...completed, id: 3, model: "claude-sonnet-5-5", created_at: queued.created_at,
      result: { ...result, general_summary: { ...result.general_summary, summary: "Sonnet's view of the Acura." } } };
    listed([done, failed, completed]);
    await qc.invalidateQueries({ queryKey: ["lot-analysis"] });  // as the analysis event does
    expect(await screen.findByText("Sonnet's view of the Acura.")).toBeInTheDocument();
    expect(historyRows()[0]).toHaveAttribute("aria-current", "true");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows a failed first analysis, with what it sent", async () => {
    show([attempt("failed", { error: "AnalysisError: the lot has no usable photos" })]);
    expect(await screen.findByText("AnalysisError: the lot has no usable photos")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Prompt and Request" })).toBeInTheDocument();
    expect(screen.queryByText(/No analysis yet/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate AI Analysis" })).toBeEnabled();
  });

  it("shows the previous analysis while a new one is generated", async () => {
    show([attempt("processing"), completed]);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "generatingAnalyzing the photos with claude-opus-5-5; this takes a minute or two. The previous analysis is shown until then.");
    expect(screen.getByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
    expect(historyRows()[0]).toHaveTextContent(/generating.*—.*—.*—.*—/);
  });

  it("says when an analysis cannot be loaded", async () => {
    show([completed]);
    vi.spyOn(api, "lotAnalysis").mockRejectedValue(new ApiError(404, "analysis not found"));
    expect(await screen.findByText("analysis not found")).toBeInTheDocument();
    expect(fact("Cost")).toHaveTextContent("$0.128");  // the history's facts still show
  });

  it("says what an analysis found nothing of", async () => {
    const empty: AnalysisResult = {
      ...result, general_summary: { ...result.general_summary, key_findings: [], inspection_priorities: [] },
      damage_analysis: { summary: "No damage visible.", observed_damage: [], possible_hidden_damage: [] },
      damaged_parts: [], image_annotations: [], limitations: [],
    };
    show([{ ...completed, result: empty, requested_by: null, request: null }]);
    expect(await screen.findByText("No damage visible in the photos.")).toBeInTheDocument();
    expect(screen.getByText("None suspected.")).toBeInTheDocument();
    expect(screen.getAllByText("None.")).toHaveLength(3);
    expect(screen.getByText("None stated.")).toBeInTheDocument();
    expect(screen.queryByText(/requested by/)).not.toBeInTheDocument();
    expect(screen.getByText(/Not recorded: this analysis was made before requests were kept/)).toBeInTheDocument();
  });

  it("shows why a request was refused", async () => {
    show([]);
    vi.spyOn(api, "requestAnalysis").mockRejectedValue(
      new ApiError(409, "an analysis of this lot is already in progress"));
    await screen.findByText(/No analysis yet/);
    await generateWith("Generate AI Analysis");
    expect(await screen.findByRole("alert")).toHaveTextContent("an analysis of this lot is already in progress");
  });

  it("offers each model with its price, the default preselected, and cancels without a request", async () => {
    show([]);
    const request = vi.spyOn(api, "requestAnalysis");
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    expect(dialog).toHaveAttribute("open");
    const opus = await within(dialog).findByRole("radio", { name: /Claude Opus 5.5/ });
    expect(opus).toBeChecked();
    expect(within(dialog).getByText(/Faster, half the price of Opus 5.5 · \$2 in \/ \$10 out per million tokens/))
      .toBeInTheDocument();
    expect(within(dialog).getAllByText(/default/)).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(dialog).not.toHaveAttribute("open");
    expect(request).not.toHaveBeenCalled();
  });

  it("says when the models cannot be loaded, and does not generate", async () => {
    show([], new Error("models unavailable"));
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    expect(await within(dialog).findByText("models unavailable")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Generate" })).toBeDisabled();
  });

  it("adds a model from the provider's list, with its price, and picks it", async () => {
    show([]);
    const listed = vi.spyOn(api, "availableModels").mockImplementation(async (provider) => ({
      models: provider === "anthropic"
        ? [{ id: "claude-haiku-5", label: "Claude Haiku 5" }] : [{ id: "gpt-5.6", label: "gpt-5.6" }],
    }));
    const haiku = { id: "claude-haiku-5", provider: "anthropic", label: "Claude Haiku 5", note: "Cheap", input_usd: 1, output_usd: 5 };
    const add = vi.spyOn(api, "addModel").mockResolvedValue({ ...haiku, custom: true });
    const request = vi.spyOn(api, "requestAnalysis").mockResolvedValue(attempt("queued"));
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "Add a model" }));

    const form = within(dialog).getByRole("form", { name: "Add a model" });
    expect(await within(form).findByText(/1 model offered by Anthropic/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("Provider"), { target: { value: "openai" } });
    expect(await within(form).findByText(/1 model offered by OpenAI/)).toBeInTheDocument();
    expect(listed).toHaveBeenLastCalledWith("openai");
    expect([...dialog.querySelectorAll("datalist option")].map((o) => o.getAttribute("value"))).toEqual(["gpt-5.6"]);
    fireEvent.change(within(form).getByLabelText("Provider"), { target: { value: "anthropic" } });
    await within(form).findByText(/1 model offered by Anthropic/);

    // choosing a listed id fills in its name
    fireEvent.change(within(form).getByLabelText("Model id"), { target: { value: "claude-haiku-5" } });
    expect(within(form).getByLabelText("Name")).toHaveValue("Claude Haiku 5");
    fireEvent.change(within(form).getByLabelText("Note"), { target: { value: "Cheap" } });
    fireEvent.change(within(form).getByLabelText(/Input/), { target: { value: "1" } });
    fireEvent.change(within(form).getByLabelText(/Output/), { target: { value: "5" } });
    vi.spyOn(api, "analysisModels").mockResolvedValue({ ...models, models: [...models.models, { ...haiku, custom: true }] });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByRole("radio", { name: /Claude Haiku 5/ })).toBeChecked();
    expect(add).toHaveBeenCalledWith(haiku);
    expect(within(dialog).getByText(/Anthropic · Cheap · \$1 in \/ \$5 out/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Generate" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("copart", "68264956", "claude-haiku-5"));
  });

  it("changes and deletes an added model, never a built-in one", async () => {
    show([], { ...models, models: [...models.models, gpt] });
    vi.spyOn(api, "availableModels").mockResolvedValue({ models: [] });
    const update = vi.spyOn(api, "updateModel").mockResolvedValue({ ...gpt, input_usd: 1.5 });
    const remove = vi.spyOn(api, "deleteModel").mockResolvedValue(undefined);
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    fireEvent.click(await within(dialog).findByRole("radio", { name: /GPT-5.6/ }));
    expect(within(dialog).getAllByRole("button", { name: /^Edit/ })).toHaveLength(1);

    fireEvent.click(within(dialog).getByRole("button", { name: "Edit GPT-5.6" }));
    const form = within(dialog).getByRole("form", { name: "Edit GPT-5.6" });
    expect(within(form).getByLabelText("Provider")).toHaveValue("openai");
    expect(within(form).getByLabelText("Model id")).toHaveValue("gpt-5.6");
    expect(await within(form).findByText(/0 models offered by OpenAI/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/Input/), { target: { value: "1.5" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("gpt-5.6", {
      id: "gpt-5.6", provider: "openai", label: "GPT-5.6", note: "", input_usd: 1.5, output_usd: 8,
    }));

    vi.spyOn(api, "analysisModels").mockResolvedValue(models);
    fireEvent.click(await within(dialog).findByRole("button", { name: "Delete GPT-5.6" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("gpt-5.6"));
    expect(await within(dialog).findByRole("radio", { name: /Claude Opus 5.5/ })).toBeChecked();  // back to the default
  });

  it("lets the id be typed when the provider's models cannot be listed, and shows why a save failed", async () => {
    show([]);
    vi.spyOn(api, "availableModels").mockRejectedValue(
      new ApiError(502, "could not list the models: set OPENAI_API_KEY in .env to list the provider's models"));
    vi.spyOn(api, "addModel").mockRejectedValue(new ApiError(409, "the model 'o4' already exists"));
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "Add a model" }));
    const form = within(dialog).getByRole("form", { name: "Add a model" });
    expect(await within(form).findByText(/set OPENAI_API_KEY in .env/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("Model id"), { target: { value: "o4" } });
    expect(within(form).getByLabelText("Name")).toHaveValue("");  // not a listed id
    fireEvent.change(within(form).getByLabelText("Name"), { target: { value: "o4" } });
    fireEvent.submit(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("the model 'o4' already exists");

    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(within(dialog).queryByRole("form", { name: "Add a model" })).not.toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: /Claude Opus 5.5/ })).toBeChecked();
    // reopening the dialog starts on the models, not on a form left open
    fireEvent.click(within(dialog).getByRole("button", { name: "Add a model" }));
    (dialog as HTMLDialogElement).close();  // Escape
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    expect(within(dialog).getByRole("button", { name: "Generate" })).toBeInTheDocument();
  });

  it("says when an added model could not be deleted", async () => {
    show([], { ...models, models: [...models.models, gpt] });
    vi.spyOn(api, "deleteModel").mockRejectedValue(new ApiError(404, "no such added model"));
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "Delete GPT-5.6" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("no such added model");
  });

  it("shows a load error and keeps the button disabled", async () => {
    show(new Error("lot not found"));
    expect(await screen.findByText("lot not found")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Generate AI Analysis" })).toBeDisabled());
  });
});

describe("tally", () => {
  it("adds up the calls, with each one's cost, and says when a model has no known price", () => {
    expect(tally(null)).toBeNull();
    expect(tally({ selection: null })).toBeNull();
    expect(tally(completed.usage)).toEqual(
      { input: 11400, output: 4100, cost: "$0.128", split: "photo selection $0.022 · analysis $0.106" });
    expect(tally({ selection: null, analysis: { model: "claude-opus-4-8", input_tokens: 10, output_tokens: 5, cost_usd: null } }))
      .toEqual({ input: 10, output: 5, cost: "unknown", split: null });
    expect(tally({ analysis: { model: "claude-opus-5-5", input_tokens: 10, output_tokens: 5 } }))
      .toEqual({ input: 10, output: 5, cost: "unknown", split: null });  // made before costs were recorded
  });
});

describe("words", () => {
  it("turns snake_case names into words", () => {
    expect(words("left_front_fender")).toBe("Left front fender");
    expect(words("")).toBe("");
  });
});
