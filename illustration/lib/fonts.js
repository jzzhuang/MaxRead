import { readFileSync } from 'fs';

const FONT_PATHS = [
  { path: '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', name: 'DejaVu Sans', weight: 400, style: 'normal' },
  { path: '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', name: 'DejaVu Sans', weight: 700, style: 'normal' },
];

let cached = null;

export function loadFonts() {
  if (cached) return cached;
  cached = FONT_PATHS.map(f => ({
    name: f.name,
    weight: f.weight,
    style: f.style,
    data: readFileSync(f.path),
  }));
  // .ttc (TrueType Collection) is not supported by satori's opentype parser.
  // CJK text will fall back to DejaVu Sans (acceptable — specs are mostly English).
  return cached;
}
