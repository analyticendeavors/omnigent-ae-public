// Stands in for upstream's `@/lib/identity` in tests that import upstream's request parser
// (serverBridge.test.ts): the parser only needs the name to exist, and the real module pulls in
// the whole app's build-time globals.
export async function authenticatedFetch(_url: string, _init?: RequestInit): Promise<Response> {
  throw new Error("authenticatedFetch is a stub in the extension's tests");
}
