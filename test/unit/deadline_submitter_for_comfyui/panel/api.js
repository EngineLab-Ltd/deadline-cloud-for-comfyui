// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// Stands in for ComfyUI's scripts/api.js. Each POST goes to the handler for its path, and
// emit() delivers a server event as ComfyUI's websocket would.
const listeners = {};
export const api = {
  // ComfyUI's ID for this tab's websocket.
  clientId: "tab-1",
  handlers: {},
  calls: [],
  addEventListener(name, listener) {
    (listeners[name] ??= []).push(listener);
  },
  emit(name, detail) {
    for (const listener of listeners[name] ?? []) {
      listener({ detail });
    }
  },
  async fetchApi(path, init) {
    const body = JSON.parse(init.body);
    api.calls.push({ path, body });
    const result = await api.handlers[path](body);
    return { ok: true, status: 200, json: async () => result };
  },
};
