// Vite's ?raw import, for tests that read a source file as text.
declare module "*?raw" {
  const text: string;
  export default text;
}
