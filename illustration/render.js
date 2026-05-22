import { readFileSync, writeFileSync } from 'fs';
import { Resvg } from '@resvg/resvg-js';
import satori from 'satori';
import { loadFonts } from './lib/fonts.js';

const WIDTH = 1600;
const HEIGHT = 900;

const TEMPLATES = {
  comparison_decomposition: () => import('./templates/comparison_decomposition.js').then(m => m.default),
};

async function main() {
  const args = process.argv.slice(2);
  let outputPath = 'output.png';
  let svgOutputPath = null;
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--output' && args[i + 1]) outputPath = args[++i];
    if (args[i] === '--svg' && args[i + 1]) svgOutputPath = args[++i];
  }

  let input = '';
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  input = Buffer.concat(chunks).toString('utf-8');

  let spec;
  try {
    spec = JSON.parse(input);
  } catch (e) {
    process.stderr.write(`Invalid JSON input: ${e.message}\n`);
    process.exit(1);
  }

  const templateName = spec.template || 'comparison_decomposition';
  if (!TEMPLATES[templateName]) {
    process.stderr.write(`Unknown template "${templateName}". Available: ${Object.keys(TEMPLATES).join(', ')}\n`);
    process.exit(1);
  }

  const fonts = loadFonts();
  const buildTree = await TEMPLATES[templateName]();
  const tree = buildTree(spec);

  const svgStr = await satori(tree, { width: WIDTH, height: HEIGHT, fonts });

  if (svgOutputPath) writeFileSync(svgOutputPath, svgStr, 'utf-8');

  const resvg = new Resvg(svgStr, {
    fitTo: { mode: 'width', value: WIDTH },
    font: { loadSystemFonts: true },
  });
  const png = resvg.render().asPng();

  writeFileSync(outputPath, png);
  process.stdout.write(JSON.stringify({ ok: true, path: outputPath }) + '\n');
}

main().catch(e => {
  process.stderr.write(`Render error: ${e.message}\n${e.stack}\n`);
  process.exit(1);
});
