// Copy picture and Download picture (2026-11-16): the clipboard takes only image/png, so a JPEG or WebP is
// drawn onto a canvas first; the write starts inside the click with the PNG still being made.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DOWNLOAD_URL_MS, PNG_TIMEOUT_MS, canCopyPicture, copyPicture, downloadPicture, revokePictureUrls, showPictureUrl } from "../src/socialVisual";

class FakeItem {
  constructor(public readonly parts: Record<string, Blob | Promise<Blob>>) {}
}

const written: FakeItem[][] = [];
let bitmap: { width: number; height: number; close: ReturnType<typeof vi.fn> };
let writeResult: () => Promise<void> = async () => undefined;

beforeEach(() => {
  written.length = 0;
  writeResult = async () => undefined;
  vi.stubGlobal("ClipboardItem", FakeItem);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { write: vi.fn((items: FakeItem[]) => (written.push(items), writeResult())) },
  });
  bitmap = { width: 4, height: 3, close: vi.fn() };
  vi.stubGlobal("createImageBitmap", vi.fn(async () => bitmap));
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ({ drawImage: vi.fn() })) as never;
  HTMLCanvasElement.prototype.toBlob = vi.fn(function (this: HTMLCanvasElement, done: BlobCallback, type?: string) {
    done(new Blob(["png"], { type }));
  }) as never;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("copyPicture", () => {
  it("writes a PNG as it is, under image/png", async () => {
    const png = new Blob(["p"], { type: "image/png" });
    expect(await copyPicture(png)).toBeNull();
    const made = await written[0]![0]!.parts["image/png"];
    expect(made).toBe(png);
    expect(createImageBitmap).not.toHaveBeenCalled();
  });

  it("starts the clipboard write at once and converts a JPEG to PNG through a canvas", async () => {
    const jpeg = new Blob(["j"], { type: "image/jpeg" });
    const pending = copyPicture(jpeg);
    // The write was handed over before the conversion finished: it holds a promise, not a blob.
    expect(written).toHaveLength(1);
    expect(Object.keys(written[0]![0]!.parts)).toEqual(["image/png"]);
    expect(await pending).toBeNull();
    const made = (await written[0]![0]!.parts["image/png"]) as Blob;
    expect(made.type).toBe("image/png");
    expect(createImageBitmap).toHaveBeenCalledWith(jpeg);
  });

  it("converts a WebP the same way", async () => {
    expect(await copyPicture(new Blob(["w"], { type: "image/webp" }))).toBeNull();
    expect(((await written[0]![0]!.parts["image/png"]) as Blob).type).toBe("image/png");
  });

  it("returns the browser's own words when the clipboard refuses", async () => {
    writeResult = async () => {
      throw new Error("Document is not focused.");
    };
    expect(await copyPicture(new Blob(["p"], { type: "image/png" }))).toBe("Document is not focused.");
  });

  it("returns a reason when the conversion fails", async () => {
    vi.stubGlobal("createImageBitmap", vi.fn(async () => Promise.reject(new Error("The image could not be decoded."))));
    writeResult = async () => {
      // The write promise rejects with the conversion's error, as the browser does.
      await written[0]![0]!.parts["image/png"];
    };
    expect(await copyPicture(new Blob(["j"], { type: "image/jpeg" }))).toBe("The image could not be decoded.");
  });

  it("says so, and offers no copy, when the browser has no image clipboard", async () => {
    vi.stubGlobal("ClipboardItem", undefined);
    expect(canCopyPicture()).toBe(false);
    expect(await copyPicture(new Blob(["p"], { type: "image/png" }))).toBe("this browser cannot copy pictures");
    expect(written).toHaveLength(0);
  });
});

describe("the PNG conversion's cleanup", () => {
  it("closes the decoded bitmap after a successful conversion", async () => {
    expect(await copyPicture(new Blob(["j"], { type: "image/jpeg" }))).toBeNull();
    await written[0]![0]!.parts["image/png"];
    expect(bitmap.close).toHaveBeenCalledTimes(1);
  });

  it("closes the bitmap when the canvas cannot be used", async () => {
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never;
    writeResult = async () => {
      await written[0]![0]!.parts["image/png"];
    };
    expect(await copyPicture(new Blob(["j"], { type: "image/jpeg" }))).toBe("Could not prepare the picture for copying; use Download.");
    expect(bitmap.close).toHaveBeenCalledTimes(1);
  });

  it("gives up after 10 seconds when toBlob never calls back, says to use Download, and still closes the bitmap", async () => {
    vi.useFakeTimers();
    try {
      HTMLCanvasElement.prototype.toBlob = vi.fn() as never;
      writeResult = async () => {
        await written[0]![0]!.parts["image/png"];
      };
      const pending = copyPicture(new Blob(["j"], { type: "image/jpeg" }));
      await vi.advanceTimersByTimeAsync(PNG_TIMEOUT_MS - 1);
      expect(bitmap.close).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(2);
      expect(await pending).toBe("Could not prepare the picture for copying; use Download.");
      expect(bitmap.close).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears the timer when toBlob answers", async () => {
    vi.useFakeTimers();
    try {
      expect(await copyPicture(new Blob(["j"], { type: "image/jpeg" }))).toBeNull();
      await written[0]![0]!.parts["image/png"];
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("object URLs", () => {
  it("revokes a row's display URL when its picture is replaced, and every one when the page is left", () => {
    let n = 0;
    const revoke = vi.fn();
    URL.createObjectURL = vi.fn(() => `blob:show-${(n += 1)}`) as never;
    URL.revokeObjectURL = revoke as never;
    expect(showPictureUrl("slot-a", new Blob(["1"]))).toBe("blob:show-1");
    expect(showPictureUrl("slot-b", new Blob(["2"]))).toBe("blob:show-2");
    expect(revoke).not.toHaveBeenCalled();
    expect(showPictureUrl("slot-a", new Blob(["3"]))).toBe("blob:show-3");
    expect(revoke).toHaveBeenCalledWith("blob:show-1");
    revokePictureUrls();
    expect(revoke).toHaveBeenCalledWith("blob:show-2");
    expect(revoke).toHaveBeenCalledWith("blob:show-3");
    expect(revoke).toHaveBeenCalledTimes(3);
  });

  it("Download makes its own URL and revokes it right after the click", () => {
    vi.useFakeTimers();
    try {
      const revoke = vi.fn();
      URL.createObjectURL = vi.fn(() => "blob:dl-1") as never;
      URL.revokeObjectURL = revoke as never;
      const spy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
      expect(downloadPicture(new Blob(["j"], { type: "image/jpeg" }), "Post")).toBe(true);
      expect(revoke).not.toHaveBeenCalled();
      vi.advanceTimersByTime(DOWNLOAD_URL_MS);
      expect(revoke).toHaveBeenCalledWith("blob:dl-1");
      spy.mockRestore();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("downloadPicture", () => {
  it("clicks a hidden link to the blob URL, named for the post", () => {
    const created = vi.fn(() => "blob:test-1");
    URL.createObjectURL = created as never;
    const clicks: HTMLAnchorElement[] = [];
    const spy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this);
    });
    expect(downloadPicture(new Blob(["j"], { type: "image/jpeg" }), "My Diagram Tool!")).toBe(true);
    expect(clicks[0]!.getAttribute("href")).toBe("blob:test-1");
    expect(clicks[0]!.download).toBe("my-diagram-tool.jpg");
    spy.mockRestore();
  });

  it("returns false where the page cannot make a blob URL", () => {
    URL.createObjectURL = undefined as never;
    expect(downloadPicture(new Blob(["j"], { type: "image/jpeg" }), "x")).toBe(false);
  });
});
