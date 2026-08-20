#!/usr/bin/env node

import { lstat, mkdir, readFile, readdir, realpath, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { transform as minifyCss } from 'lightningcss';
import { minify as minifyJavaScript } from 'terser';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(SCRIPT_DIR, '..', '..');
const DEFAULT_INPUT = path.join(PROJECT_ROOT, 'app', 'static');
const DEFAULT_OUTPUT = path.join(PROJECT_ROOT, 'dist', 'static');
const OUTPUT_MARKER = 'mediaflux-static-output-v1\n';

function parseArguments(argv) {
  const options = { input: DEFAULT_INPUT, output: DEFAULT_OUTPUT };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--input' || argument === '--output') {
      const value = argv[index + 1];
      if (!value || value.startsWith('--')) {
        throw new Error(`${argument} requires a path`);
      }
      options[argument.slice(2)] = path.resolve(value);
      index += 1;
      continue;
    }
    if (argument === '--help' || argument === '-h') {
      console.log('Usage: npm run build:static -- [--input PATH] [--output PATH]');
      process.exit(0);
    }
    throw new Error(`unknown argument: ${argument}`);
  }
  return options;
}

function isWithin(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative !== '' && !relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative);
}

async function listFiles(root) {
  const files = [];
  async function visit(directory) {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        await visit(absolute);
      } else if (entry.isFile()) {
        files.push(absolute);
      }
    }
  }
  await visit(root);
  return files;
}

async function minifyJs(source, filename) {
  const result = await minifyJavaScript({ [filename]: source }, {
    ecma: 2020,
    module: false,
    compress: {
      defaults: true,
      module: false,
      toplevel: false,
      unsafe: false,
    },
    mangle: {
      toplevel: false,
    },
    format: {
      comments: false,
    },
    sourceMap: false,
  });
  if (typeof result.code !== 'string') {
    throw new Error(`terser did not emit JavaScript for ${filename}`);
  }
  return Buffer.from(`${result.code}\n`);
}

function minifyStylesheet(source, filename) {
  const result = minifyCss({
    filename,
    code: source,
    minify: true,
    sourceMap: false,
  });
  return Buffer.concat([result.code, Buffer.from('\n')]);
}

async function prepareOutputDirectory(input, output) {
  const inputReal = await realpath(input);
  const outputParent = path.dirname(output);
  await mkdir(outputParent, { recursive: true });
  const outputParentReal = await realpath(outputParent);
  const outputReal = path.join(outputParentReal, path.basename(output));
  if (inputReal === outputReal || isWithin(inputReal, outputReal) || isWithin(outputReal, inputReal)) {
    throw new Error('input and output directories must not overlap');
  }

  const marker = path.join(outputParentReal, `.${path.basename(output)}.mediaflux-static-output`);
  const outputInfo = await lstat(outputReal).catch(() => null);
  if (outputInfo) {
    if (!outputInfo.isDirectory() || outputInfo.isSymbolicLink()) {
      throw new Error(`refusing to replace non-directory output: ${output}`);
    }
    const markerValue = await readFile(marker, 'utf8').catch(() => '');
    if (markerValue !== OUTPUT_MARKER) {
      throw new Error(`refusing to remove unowned output directory: ${output}`);
    }
    await rm(outputReal, { recursive: true, force: true });
  }

  await mkdir(outputReal, { recursive: true });
  await writeFile(marker, OUTPUT_MARKER, { encoding: 'utf8', mode: 0o600 });
  return outputReal;
}

async function buildStaticAssets(input, output) {
  const inputInfo = await stat(input).catch(() => null);
  if (!inputInfo?.isDirectory()) {
    throw new Error(`static input directory does not exist: ${input}`);
  }
  const preparedOutput = await prepareOutputDirectory(input, output);

  const files = await listFiles(input);
  const summary = {
    files: files.length,
    js: 0,
    css: 0,
    copied: 0,
    inputBytes: 0,
    outputBytes: 0,
  };

  for (const sourcePath of files) {
    const relativePath = path.relative(input, sourcePath);
    const outputPath = path.join(preparedOutput, relativePath);
    const source = await readFile(sourcePath);
    let built = source;

    if (relativePath.endsWith('.css') && !relativePath.endsWith('.min.css')) {
      built = minifyStylesheet(source, relativePath);
      summary.css += 1;
    } else if (relativePath.endsWith('.js') && !relativePath.endsWith('.min.js')) {
      built = await minifyJs(source.toString('utf8'), relativePath);
      summary.js += 1;
    } else {
      summary.copied += 1;
    }

    await mkdir(path.dirname(outputPath), { recursive: true });
    await writeFile(outputPath, built);
    summary.inputBytes += source.byteLength;
    summary.outputBytes += built.byteLength;
  }

  const savedBytes = summary.inputBytes - summary.outputBytes;
  const savedPercent = summary.inputBytes
    ? ((savedBytes / summary.inputBytes) * 100).toFixed(1)
    : '0.0';
  console.log(
    `Static assets: ${summary.files} files (${summary.js} JS, ${summary.css} CSS, ${summary.copied} copied), `
      + `${summary.inputBytes} -> ${summary.outputBytes} bytes, saved ${savedBytes} bytes (${savedPercent}%).`,
  );
}

try {
  const options = parseArguments(process.argv.slice(2));
  await buildStaticAssets(options.input, options.output);
} catch (error) {
  console.error(`Static asset build failed: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}
