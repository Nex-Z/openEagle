const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const root = path.resolve(__dirname, "..");
const outDir = path.join(root, "build");
const sizes = [16, 32, 48, 64, 128, 256];

function crc32(buffer) {
  let crc = -1;
  for (const byte of buffer) {
    crc ^= byte;
    for (let i = 0; i < 8; i++) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return (crc ^ -1) >>> 0;
}

function chunk(type, data) {
  const name = Buffer.from(type);
  const out = Buffer.alloc(12 + data.length);
  out.writeUInt32BE(data.length, 0);
  name.copy(out, 4);
  data.copy(out, 8);
  out.writeUInt32BE(crc32(Buffer.concat([name, data])), 8 + data.length);
  return out;
}

function pngFromRgba(width, height, rgba) {
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) {
    const row = y * (width * 4 + 1);
    raw[row] = 0;
    rgba.copy(raw, row + 1, y * width * 4, (y + 1) * width * 4);
  }

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;

  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function over(bottom, top) {
  const a = top[3] + bottom[3] * (1 - top[3]);
  if (a <= 0) return [0, 0, 0, 0];
  return [
    (top[0] * top[3] + bottom[0] * bottom[3] * (1 - top[3])) / a,
    (top[1] * top[3] + bottom[1] * bottom[3] * (1 - top[3])) / a,
    (top[2] * top[3] + bottom[2] * bottom[3] * (1 - top[3])) / a,
    a,
  ];
}

function mix(a, b, t) {
  return [
    a[0] + (b[0] - a[0]) * t,
    a[1] + (b[1] - a[1]) * t,
    a[2] + (b[2] - a[2]) * t,
    a[3] + (b[3] - a[3]) * t,
  ];
}

function roundedRectSdf(x, y, cx, cy, hx, hy, r) {
  const qx = Math.abs(x - cx) - hx + r;
  const qy = Math.abs(y - cy) - hy + r;
  return Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - r;
}

function inPoly(x, y, points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const xi = points[i][0];
    const yi = points[i][1];
    const xj = points[j][0];
    const yj = points[j][1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function mirror(points) {
  return points.map(([x, y]) => [256 - x, y]);
}

const leftWing = [
  [40, 132],
  [108, 65],
  [132, 88],
  [103, 117],
  [154, 118],
  [112, 150],
  [69, 176],
  [88, 146],
  [40, 147],
];
const rightWing = mirror(leftWing);
const body = [
  [128, 74],
  [153, 111],
  [143, 169],
  [128, 198],
  [113, 169],
  [103, 111],
];
const beak = [
  [136, 74],
  [181, 89],
  [145, 102],
];
const openCut = [
  [128, 88],
  [140, 118],
  [128, 144],
  [116, 118],
];
const leftHighlight = [
  [74, 128],
  [108, 91],
  [119, 101],
  [97, 124],
];
const rightHighlight = mirror(leftHighlight);

function colorAt(x, y) {
  const bgSdf = roundedRectSdf(x, y, 128, 128, 112, 112, 46);
  if (bgSdf > 0) return [0, 0, 0, 0];

  const bgTop = [8, 17, 34, 1];
  const bgBottom = [9, 90, 104, 1];
  const glow = [25, 153, 170, 0.22 * Math.max(0, 1 - Math.hypot(x - 178, y - 64) / 160)];
  let color = over(mix(bgTop, bgBottom, y / 256), glow);

  if (bgSdf > -5) {
    color = over(color, [255, 255, 255, 0.16]);
  }

  if (inPoly(x, y, leftWing)) color = over(color, [244, 211, 94, 1]);
  if (inPoly(x, y, rightWing)) color = over(color, [247, 179, 43, 1]);
  if (inPoly(x, y, body)) color = over(color, [255, 239, 178, 1]);
  if (inPoly(x, y, beak)) color = over(color, [255, 107, 53, 1]);
  if (inPoly(x, y, leftHighlight)) color = over(color, [255, 255, 255, 0.24]);
  if (inPoly(x, y, rightHighlight)) color = over(color, [255, 255, 255, 0.18]);
  if (inPoly(x, y, openCut)) color = over(color, [9, 43, 61, 0.92]);

  const eye = Math.hypot(x - 143, y - 88);
  if (eye < 3.7) color = over(color, [10, 24, 38, 1]);

  return color;
}

function render(size) {
  const samples = size <= 32 ? 4 : 3;
  const rgba = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < samples; sy++) {
        for (let sx = 0; sx < samples; sx++) {
          const px = ((x + (sx + 0.5) / samples) / size) * 256;
          const py = ((y + (sy + 0.5) / samples) / size) * 256;
          const c = colorAt(px, py);
          r += c[0] * c[3];
          g += c[1] * c[3];
          b += c[2] * c[3];
          a += c[3];
        }
      }
      const total = samples * samples;
      const alpha = a / total;
      const offset = (y * size + x) * 4;
      rgba[offset] = alpha ? Math.round(r / a) : 0;
      rgba[offset + 1] = alpha ? Math.round(g / a) : 0;
      rgba[offset + 2] = alpha ? Math.round(b / a) : 0;
      rgba[offset + 3] = Math.round(alpha * 255);
    }
  }
  return pngFromRgba(size, size, rgba);
}

function icoFromPngs(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);

  const entries = [];
  let offset = header.length + images.length * 16;
  for (const image of images) {
    const entry = Buffer.alloc(16);
    entry[0] = image.size === 256 ? 0 : image.size;
    entry[1] = image.size === 256 ? 0 : image.size;
    entry[2] = 0;
    entry[3] = 0;
    entry.writeUInt16LE(1, 4);
    entry.writeUInt16LE(32, 6);
    entry.writeUInt32LE(image.png.length, 8);
    entry.writeUInt32LE(offset, 12);
    offset += image.png.length;
    entries.push(entry);
  }
  return Buffer.concat([header, ...entries, ...images.map((image) => image.png)]);
}

fs.mkdirSync(outDir, { recursive: true });
const images = sizes.map((size) => ({ size, png: render(size) }));
fs.writeFileSync(path.join(outDir, "icon.png"), images.at(-1).png);
fs.writeFileSync(path.join(outDir, "icon.ico"), icoFromPngs(images));
console.log(`Wrote ${path.join(outDir, "icon.png")}`);
console.log(`Wrote ${path.join(outDir, "icon.ico")}`);
