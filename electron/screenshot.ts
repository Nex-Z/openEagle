import fs from "node:fs";
import path from "node:path";
import os from "node:os";

export interface ScreenshotResult {
  path: string;
  width: number;
  height: number;
  capturedAt: string;
}

export async function captureScreenshot(): Promise<ScreenshotResult> {
  // Dynamic import to avoid startup issues if nut-js has native binding problems
  const { screen } = await import("@nut-tree-fork/nut-js");
  const image = await screen.grab();

  const timestamp = Date.now();
  const targetPath = path.join(os.tmpdir(), `open_eagle_solo_${timestamp}.png`);

  // nut-js image → PNG buffer
  const pngBuffer = imageToPngBuffer(image);
  fs.writeFileSync(targetPath, pngBuffer);

  return {
    path: targetPath,
    width: image.width,
    height: image.height,
    capturedAt: new Date().toISOString(),
  };
}

function imageToPngBuffer(image: { width: number; height: number; data: Buffer }): Buffer {
  // nut-js returns raw BGRA pixel data; encode as PNG manually
  // Use a minimal PNG encoder for the raw pixel data
  const { width, height, data } = image;
  return encodePng(width, height, data);
}

// Minimal PNG encoder for raw BGRA pixel data
function encodePng(width: number, height: number, bgraData: Buffer): Buffer {
  const channels = 4;
  const rowBytes = width * channels;
  const rawRows: Buffer[] = [];

  for (let y = 0; y < height; y++) {
    const row = Buffer.alloc(1 + rowBytes);
    row[0] = 0; // filter: none
    for (let x = 0; x < width; x++) {
      const srcOffset = (y * width + x) * channels;
      const dstOffset = 1 + x * channels;
      row[dstOffset] = bgraData[srcOffset + 2];     // R (from B)
      row[dstOffset + 1] = bgraData[srcOffset + 1]; // G
      row[dstOffset + 2] = bgraData[srcOffset];     // B (from R)
      row[dstOffset + 3] = bgraData[srcOffset + 3]; // A
    }
    rawRows.push(row);
  }

  const rawData = Buffer.concat(rawRows);
  const zlib = require("node:zlib") as typeof import("node:zlib");
  const compressed = zlib.deflateSync(rawData);

  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

  function chunk(type: string, data: Buffer): Buffer {
    const typeBytes = Buffer.from(type, "ascii");
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length, 0);
    const crcData = Buffer.concat([typeBytes, data]);
    const crc = crc32(crcData);
    const crcBuf = Buffer.alloc(4);
    crcBuf.writeUInt32BE(crc >>> 0, 0);
    return Buffer.concat([length, typeBytes, data, crcBuf]);
  }

  // IHDR
  const ihdrData = Buffer.alloc(13);
  ihdrData.writeUInt32BE(width, 0);
  ihdrData.writeUInt32BE(height, 4);
  ihdrData[8] = 8;  // bit depth
  ihdrData[9] = 6;  // color type: RGBA
  ihdrData[10] = 0; // compression
  ihdrData[11] = 0; // filter
  ihdrData[12] = 0; // interlace

  // IDAT
  const idatChunk = chunk("IDAT", compressed);

  // IEND
  const iendChunk = chunk("IEND", Buffer.alloc(0));

  return Buffer.concat([signature, chunk("IHDR", ihdrData), idatChunk, iendChunk]);
}

function crc32(buf: Buffer): number {
  let crc = 0xffffffff;
  for (let i = 0; i < buf.length; i++) {
    crc ^= buf[i];
    for (let j = 0; j < 8; j++) {
      if (crc & 1) {
        crc = (crc >>> 1) ^ 0xedb88320;
      } else {
        crc = crc >>> 1;
      }
    }
  }
  return crc ^ 0xffffffff;
}
