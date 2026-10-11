import { readFileSync, readdirSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

// Run after installing npm/Python dependencies and fetching Cargo.lock.
// Uses the installed release dependencies, never personal settings or secrets.
// Upstream fallback files are pinned to the published crate's git commit.
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = join(root, "src/app/content/third-party-notices.txt");
const target = "x86_64-pc-windows-msvc";
const components = [];
const normalize = (text) => text.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").trim();
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const licenseName = /^(licen[sc]e|copying|copyright|notice)/i;

function command(executable, args, cwd = root) {
  const result = spawnSync(executable, args, { cwd, encoding: "utf8", maxBuffer: 100 * 1024 * 1024 });
  if (result.error || result.status !== 0) {
    throw new Error(`${executable} failed: ${result.error?.message ?? result.stderr.trim()}`);
  }
  return result.stdout;
}

function licenseFiles(directory) {
  const found = [];
  for (const entry of readdirSync(directory, { withFileTypes: true }).sort((a, b) => compare(a.name, b.name))) {
    const path = join(directory, entry.name);
    if (entry.isFile() && licenseName.test(entry.name)) found.push(path);
    if (entry.isDirectory() && /^(licenses?|notices?)$/i.test(entry.name)) {
      const walk = (base) => {
        for (const child of readdirSync(base, { withFileTypes: true }).sort((a, b) => compare(a.name, b.name))) {
          const childPath = join(base, child.name);
          if (child.isDirectory()) walk(childPath);
          else found.push(childPath);
        }
      };
      walk(path);
    }
  }
  return found;
}

function add(name, version, license, source, texts) {
  const notices = [...new Set(texts.map(normalize).filter(Boolean))];
  if (!notices.length) throw new Error(`No full license notice found for ${name} ${version}.`);
  components.push({ name, version, license: license ?? "See accompanying notice", source, notices });
}

const downloads = new Map();
async function download(url) {
  if (!downloads.has(url)) downloads.set(url, (async () => {
    const response = await fetch(url, { signal: AbortSignal.timeout(30000) });
    if (!response.ok) throw new Error(`Could not fetch license ${url}: HTTP ${response.status}.`);
    return normalize(await response.text());
  })());
  return downloads.get(url);
}

function javascriptNotices() {
  const lock = JSON.parse(readFileSync(join(root, "package-lock.json"), "utf8"));
  for (const [path, info] of Object.entries(lock.packages).sort(([a], [b]) => compare(a, b))) {
    // Tailwind is installed as a build tool, but its base stylesheet ships.
    if (!path || (info.dev && path !== "node_modules/tailwindcss")) continue;
    const directory = join(root, path);
    const installed = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
    if (installed.version !== info.version) throw new Error(`Run npm ci: ${installed.name} differs from package-lock.json.`);
    add(`JavaScript: ${installed.name}`, info.version, info.license,
      `https://www.npmjs.com/package/${installed.name}/v/${info.version}`,
      licenseFiles(directory).map((file) => readFileSync(file, "utf8")));
  }
}

function pythonNotices() {
  const python = process.env.HUEPHONY_PYTHON ?? join(root, "backend/.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const inventory = JSON.parse(command(python, ["-c", String.raw`
import importlib.metadata as metadata
import json
import pathlib
import re
import sys
from packaging.requirements import Requirement

root = pathlib.Path(sys.argv[1])
seen = {}
pending = []
for line in (root / "backend/requirements.txt").read_text().splitlines():
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        continue
    requirement = Requirement(line)
    if requirement.name.lower() not in {"pytest", "pyinstaller"}:
        pending.append(requirement)
while pending:
    requirement = pending.pop()
    if requirement.marker and not requirement.marker.evaluate():
        continue
    distribution = metadata.distribution(requirement.name)
    if requirement.specifier and distribution.version not in requirement.specifier:
        raise RuntimeError(f"Install backend requirements: {requirement.name} has the wrong version.")
    key = distribution.metadata["Name"].lower().replace("_", "-")
    extras = set(requirement.extras)
    if key in seen and extras <= seen[key][1]:
        continue
    if key in seen:
        extras |= seen[key][1]
    seen[key] = (distribution, extras)
    for value in distribution.requires or []:
        dependency = Requirement(value)
        if dependency.marker and not any(dependency.marker.evaluate({"extra": extra}) for extra in extras | {""}):
            continue
        dependency.marker = None
        pending.append(dependency)

# The PyInstaller bootloader is shipped, although its build dependencies are not.
seen["pyinstaller"] = (metadata.distribution("pyinstaller"), set())
packages = []
for distribution, _ in seen.values():
    files = []
    entries = list(distribution.files or [])
    declared = distribution.metadata.get_all("License-File", [])
    for entry in entries:
        text = str(entry).replace("\\", "/")
        basename = pathlib.PurePosixPath(text).name
        is_notice = bool(re.match(r"^(licen[sc]e|copying|copyright|notice)", basename, re.I))
        is_declared = any(text.endswith("/" + value.replace("\\", "/")) for value in declared)
        if is_notice or is_declared:
            path = pathlib.Path(distribution.locate_file(entry))
            if path.is_file():
                files.append(str(path))
    packages.append({
        "name": distribution.metadata["Name"], "version": distribution.version,
        "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
        "files": sorted(set(files)),
    })
prefix = pathlib.Path(sys.base_prefix)
runtime_licenses = [str(prefix / name) for name in ("LICENSE.txt", "LICENSE") if (prefix / name).is_file()]
print(json.dumps({"packages": packages, "runtime_version": sys.version.split()[0], "runtime_licenses": runtime_licenses}))
`, root]));
  for (const info of inventory.packages.sort((a, b) => compare(a.name, b.name))) {
    add(`Python: ${info.name}`, info.version, info.license,
      `https://pypi.org/project/${info.name}/${info.version}/`,
      info.files.map((file) => readFileSync(file, "utf8")));
  }
  add("Python runtime", inventory.runtime_version, "PSF-2.0 and accompanying licenses",
    `https://www.python.org/downloads/release/python-${inventory.runtime_version.replaceAll(".", "")}/`,
    inventory.runtime_licenses.map((file) => readFileSync(file, "utf8")));
}

async function rustNotices() {
  const metadata = JSON.parse(command("cargo", ["metadata", "--locked", "--offline", "--format-version", "1", "--filter-platform", target], join(root, "src-tauri")));
  const packages = new Map(metadata.packages.map((entry) => [entry.id, entry]));
  const nodes = new Map(metadata.resolve.nodes.map((entry) => [entry.id, entry]));
  const runtime = new Set();
  function visit(id) {
    // Procedural macros run during compilation and are not part of the app.
    if (runtime.has(id) || packages.get(id).targets.some((entry) => entry.kind.includes("proc-macro"))) return;
    runtime.add(id);
    for (const dependency of nodes.get(id).deps) {
      if (dependency.dep_kinds.some((kind) => kind.kind === null)) visit(dependency.pkg);
    }
  }
  visit(metadata.resolve.root);
  for (const info of [...packages.values()].filter((entry) => runtime.has(entry.id) && entry.id !== metadata.resolve.root).sort((a, b) => compare(`${a.name}@${a.version}`, `${b.name}@${b.version}`))) {
    const directory = dirname(info.manifest_path);
    const files = licenseFiles(directory);
    if (info.license_file) {
      const file = resolve(directory, info.license_file);
      if (!files.includes(file)) files.push(file);
    }
    let texts = files.map((file) => readFileSync(file, "utf8"));
    if (!texts.length) {
      const vcsFile = join(directory, ".cargo_vcs_info.json");
      if (!existsSync(vcsFile)) throw new Error(`No published license or git revision for ${info.name}.`);
      const vcs = JSON.parse(readFileSync(vcsFile, "utf8"));
      const github = info.repository?.match(/^https:\/\/github\.com\/([^/]+\/[^/]+?)(?:\.git)?\/?$/)?.[1];
      if (!github) throw new Error(`Add a verified upstream license fallback for ${info.name}.`);
      const base = `https://raw.githubusercontent.com/${github}/${vcs.git.sha1}/`;
      if (info.name === "alloc-stdlib" || info.name.startsWith("webview2-com")) {
        texts = [await download(`${base}LICENSE`)];
      } else if (info.name.startsWith("unic-")) {
        texts = [await download(`${base}COPYRIGHT.md`), await download(`${base}LICENSE-MIT`)];
      } else if (info.name === "selectors") {
        texts = [await download("https://www.mozilla.org/media/MPL/2.0/index.txt")];
      } else {
        throw new Error(`Add a verified upstream license fallback for ${info.name} ${info.version}.`);
      }
    }
    add(`Rust: ${info.name}`, info.version, info.license,
      `https://crates.io/crates/${info.name}/${info.version}`, texts);
  }
}

const mitPermission = `Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.`;

async function main() {
  javascriptNotices();
  pythonNotices();
  await rustNotices();
  add("Shader: 2D simplex noise", "adapted", "MIT", "https://github.com/ashima/webgl-noise",
    [`Copyright (C) 2011 by Ashima Arts (Simplex noise)\nCopyright (C) 2011-2016 by Stefan Gustavson (Classic noise and others)\n\n${mitPermission}`]);
  add("Shader: Hash without Sine", "adapted", "MIT", "https://www.shadertoy.com/view/4djSRW",
    [`Copyright (c) 2014 David Hoskins.\n\n${mitPermission}`]);
  components.sort((a, b) => compare(`${a.name}@${a.version}`, `${b.name}@${b.version}`));
  const groups = new Map();
  for (const component of components) {
    for (const text of component.notices) {
      if (!groups.has(text)) groups.set(text, []);
      groups.get(text).push(`${component.name} ${component.version}`);
    }
  }
  const lines = [
    "Huephony — third-party notices", "",
    "Generated by scripts/generate-notices.mjs. Keep this file with distributed builds.",
    "Huephony's GPLv3 license does not replace the licenses of these components.",
    "This inventory covers JavaScript production dependencies, the Tailwind stylesheet,",
    "Python runtime requirements and their dependencies, the Python/PyInstaller runtimes,",
    "Windows x64 Rust runtime dependencies, bundled fonts, and adapted shader helpers.",
    "Build/test tools and procedural macros are excluded. Platform components such as",
    "Windows and the Microsoft Edge WebView2 Runtime have their own terms.", "",
    "COMPONENTS", "==========", "",
    ...components.flatMap((entry) => [`${entry.name} ${entry.version}`, `License: ${entry.license}`, `Source: ${entry.source}`, ""]),
    "FULL COPYRIGHT AND LICENSE NOTICES", "==================================", "",
  ];
  let index = 0;
  for (const [text, names] of groups) {
    lines.push(`Notice ${++index}`, "-".repeat(72), "Applies to:", ...names.map((name) => `  ${name}`), "", text, "");
  }
  const text = `${lines.join("\n").trimEnd()}\n`;
  if (process.argv.includes("--check")) {
    if (!existsSync(output) || readFileSync(output, "utf8") !== text) throw new Error("Third-party notices are stale. Run node scripts/generate-notices.mjs and commit the updated asset.");
    console.log(`Notices match ${components.length} components.`);
    return;
  }
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, text, "utf8");
  console.log(`Generated ${components.length} components and ${groups.size} unique notices (${Buffer.byteLength(text)} bytes).`);
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
