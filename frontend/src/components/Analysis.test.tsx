import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import {
  api, ApiError, type AnalysisResult, type LotAnalyses, type SentRequest, type VehicleAnalysis,
} from "../lib/api";
import { AnalysisButton, AnalysisSection, spent, words } from "./Analysis";

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

function show(data: LotAnalyses | Error, offered: typeof models | Error = models) {
  const list = vi.spyOn(api, "analysisModels");
  if (offered instanceof Error) list.mockRejectedValue(offered);
  else list.mockResolvedValue(offered);
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
    const dialog = await generateWith("Generate AI Analysis", "Claude Sonnet 5.5");
    expect(dialog).not.toHaveAttribute("open");
    expect(await screen.findByRole("button", { name: "Generating AI Analysis…" })).toBeDisabled();
    expect(request).toHaveBeenCalledWith("copart", "68264956", "claude-sonnet-5-5");
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
    expect(screen.getByText(
      /Used 15,500 tokens \(11,400 in, 4,100 out\) · \$0\.128 \(photo selection \$0\.022, analysis \$0\.106\)/,
    )).toBeInTheDocument();
    expect(screen.getByText("Raw analysis JSON").closest("details")).toHaveTextContent('"schema_version": "1.0"');
  });

  it("shows the settings, prompts and request parameters each model call was sent", async () => {
    show({ latest: completed, current: completed });
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
      error: "AIError: the answer is not valid JSON", completed_at: "2026-09-28T11:00:00Z",
      usage: { analysis: { model: "claude-opus-5-5", input_tokens: 800, output_tokens: 200, cost_usd: 0.0072 } },
    }) });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /The latest analysis failed .*: AIError: the answer is not valid JSON It used 1,000 tokens \(800 in, 200 out\) · \$0\.007\. The previous analysis is shown\./);
    expect(screen.getByText("2013 Acura TSX with a heavy front impact.")).toBeInTheDocument();

    vi.spyOn(api, "requestAnalysis").mockResolvedValue(attempt("queued", { id: 3 }));
    load.mockResolvedValue({ latest: completed, current: attempt("queued", { id: 3 }) });
    await generateWith("Regenerate AI Analysis");
    expect(api.requestAnalysis).toHaveBeenCalledWith("copart", "68264956", "claude-opus-5-5");
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
    show({ latest: { ...completed, result: empty, requested_by: null, request: null }, current: null });
    expect(await screen.findByText("No damage visible in the photos.")).toBeInTheDocument();
    expect(screen.getByText("None suspected.")).toBeInTheDocument();
    expect(screen.getAllByText("None.")).toHaveLength(3);
    expect(screen.getByText("None stated.")).toBeInTheDocument();
    expect(screen.queryByText(/requested by/)).not.toBeInTheDocument();
    expect(screen.getByText(/Not recorded: this analysis was made before requests were kept/)).toBeInTheDocument();
  });

  it("shows why a request was refused", async () => {
    show({ latest: null, current: null });
    vi.spyOn(api, "requestAnalysis").mockRejectedValue(
      new ApiError(409, "an analysis of this lot is already in progress"));
    await screen.findByText(/No analysis yet/);
    await generateWith("Generate AI Analysis");
    expect(await screen.findByRole("alert")).toHaveTextContent("an analysis of this lot is already in progress");
  });

  it("offers each model with its price, the default preselected, and cancels without a request", async () => {
    show({ latest: null, current: null });
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
    show({ latest: null, current: null }, new Error("models unavailable"));
    await screen.findByText(/No analysis yet/);
    fireEvent.click(screen.getByRole("button", { name: "Generate AI Analysis" }));
    const dialog = screen.getByRole("dialog", { name: "Generate AI Analysis" });
    expect(await within(dialog).findByText("models unavailable")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Generate" })).toBeDisabled();
  });

  it("adds a model from the provider's list, with its price, and picks it", async () => {
    show({ latest: null, current: null });
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
    show({ latest: null, current: null }, { ...models, models: [...models.models, gpt] });
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
    show({ latest: null, current: null });
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
    show({ latest: null, current: null }, { ...models, models: [...models.models, gpt] });
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

describe("spent", () => {
  it("adds up the calls, and says when a model has no known price", () => {
    expect(spent(null)).toBeNull();
    expect(spent({ selection: null })).toBeNull();
    expect(spent({ selection: null, analysis: { model: "claude-opus-4-8", input_tokens: 10, output_tokens: 5, cost_usd: null } }))
      .toBe("15 tokens (10 in, 5 out) · cost unknown");
    expect(spent({ analysis: { model: "claude-opus-5-5", input_tokens: 10, output_tokens: 5 } }))
      .toBe("15 tokens (10 in, 5 out) · cost unknown");  // made before costs were recorded
  });
});

describe("words", () => {
  it("turns snake_case names into words", () => {
    expect(words("left_front_fender")).toBe("Left front fender");
    expect(words("")).toBe("");
  });
});
