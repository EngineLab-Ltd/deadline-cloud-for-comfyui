// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Runs one scenario against the real deadline_ui.js with ComfyUI stubbed out, and prints what
// happened as JSON. test_panel.py lays out the files and checks the result.
import { webcrypto } from "node:crypto";
import { app } from "./scripts/app.js";
import { api } from "./scripts/api.js";

globalThis.crypto ??= webcrypto;
const ui = await import("./extension/web/deadline_ui.js");
const state = ui.__state;
ui.listenForSubmissions();

const settle = () => new Promise((resolve) => setTimeout(resolve, 10));
function deferred() {
  let resolve;
  const promise = new Promise((done) => (resolve = done));
  return { promise, resolve };
}
function planFor(body) {
  const image = body.workflow[1].inputs.image;
  return {
    name: "fox",
    parameters: [],
    values: {},
    sources: [],
    attachments: { filenames: [`/input/${image}`], directories: [] },
  };
}
function jobs() {
  return api.calls
    .filter(({ path }) => path === "/deadline/jobs")
    .map(({ body }) => ({
      image: body.workflow[1].inputs.image,
      attachments: body.attachments.filenames,
      submissionId: body.submissionId,
      clientId: body.clientId,
    }));
}

api.handlers["/deadline/cloud/status"] = async () => ({
  authenticationStatus: "AUTHENTICATED",
  farmId: "farm-1",
  queueId: "queue-1",
});
api.handlers["/deadline/cloud/farms"] = async () => ({ farms: [] });
api.handlers["/deadline/plan"] = async (body) => planFor(body);
api.handlers["/deadline/jobs"] = async (body) => ({ submissionId: body.submissionId });
await ui.loadAccount(true);
await ui.loadPlan(true);

const result = {};
const scenario = process.argv[2];

if (scenario === "edited-before-replanning") {
  // The artist edits the graph and submits before the debounced plan request starts.
  app.image = "new.png";
  await ui.submit();
  await settle();
  result.jobsAfterEdit = jobs();
  result.replanned = api.calls.filter(({ path }) => path === "/deadline/plan").length === 2;
  result.planClientIds = api.calls
    .filter(({ path }) => path === "/deadline/plan")
    .map(({ body }) => body.clientId);
  await ui.submit();
  result.jobs = jobs();
} else if (scenario === "edited-while-planning") {
  // The plan for the edited graph is still loading when the artist submits.
  app.image = "new.png";
  const slow = deferred();
  api.handlers["/deadline/plan"] = async (body) => {
    await slow.promise;
    return planFor(body);
  };
  const planning = ui.loadPlan();
  await settle();
  result.canSubmitWhilePlanning = Boolean(ui.canSubmit());
  await ui.submit();
  result.jobsWhilePlanning = jobs();
  slow.resolve();
  await planning;
  await ui.submit();
  result.jobs = jobs();
} else if (scenario === "queue-template-value") {
  // Conda Packages is a queue parameter that the job template also defines, as comfy.
  state.queue = {
    parameters: [{ name: "CondaPackages", type: "STRING", default: "" }],
    storageProfiles: [],
  };
  state.plan.parameters = [
    { name: "CondaPackages", type: "STRING", default: "comfy", userInterface: { control: "HIDDEN" } },
  ];
  const sent = async () => {
    state.submission = null;
    await ui.submit();
    await settle();
    return api.calls.at(-1).body.parameters.CondaPackages ?? null;
  };
  const field = state.queue.parameters[0];
  result.shown = ui.__queueParameterValue(field);
  result.unedited = await sent();
  ui.editQueueParameter("CondaPackages", "comfy my-nodes");
  result.edited = await sent();
  ui.editQueueParameter("CondaPackages", "");
  result.cleared = await sent();
  result.shownAfterClearing = ui.__queueParameterValue(field);
} else if (scenario.startsWith("queue-")) {
  // The queue environment's Conda Channels, edited and then cleared or reset in the panel.
  state.queue = {
    parameters: [{ name: "CondaChannels", type: "STRING", default: "deadline-cloud" }],
    storageProfiles: [],
  };
  if (scenario === "queue-configured") {
    state.plan.queueValues = { CondaChannels: "s3://bucket/Conda/Default conda-forge" };
  }
  ui.editQueueParameter("CondaChannels", "my-channel");
  await ui.submit();
  await settle();
  result.edited = api.calls.at(-1).body.parameters.CondaChannels ?? null;
  state.submission = null;
  ui.editQueueParameter("CondaChannels", "");
  await ui.submit();
  await settle();
  result.cleared = "CondaChannels" in api.calls.at(-1).body.parameters
    ? api.calls.at(-1).body.parameters.CondaChannels
    : null;
} else if (scenario.startsWith("early-")) {
  // The server's event arrives before the response to the submission request.
  const response = deferred();
  api.handlers["/deadline/jobs"] = async (body) => {
    await response.promise;
    return { submissionId: body.submissionId };
  };
  const submitting = ui.submit();
  await settle();
  const [{ submissionId }] = jobs();
  const detail = { submissionId, name: "ComfyUI - fox" };
  try {
    if (scenario === "early-confirm") {
      api.emit("deadline.confirm", { ...detail, message: "Upload 5 GB?", default: true });
    } else {
      api.emit("deadline.submitted", { ...detail, status: "SUBMITTED", jobId: "job-1" });
    }
  } catch {
    // Without a document, opening the dialog for the confirmation fails after it's recorded.
  }
  response.resolve();
  await submitting;
  result.submission = state.submission;
  result.canSubmit = Boolean(ui.canSubmit());
  result.toasts = app.toasts.map(({ severity }) => severity);
}

console.log(JSON.stringify(result));
process.exit(0);
