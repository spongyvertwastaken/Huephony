import { useEffect, useRef, useState } from "react";
import { GLASS_UNIFORMS, glassSource, type GlassStyle } from "./glass";

export interface SceneColor { hex: string; weight: number }

const BLOBS = 6;
const FRAME_MS = 1000 / 30;
const ART_SIZE = 64;
const GLASS_FADE_S = .9;

const vertexSource = `
attribute vec2 position;
varying vec2 vUv;
void main() { vUv = position * .5 + .5; gl_Position = vec4(position, 0., 1.); }
`;

const colorMath = `
vec3 srgbToLinear(vec3 c) { return mix(c / 12.92, pow((c + .055) / 1.055, vec3(2.4)), step(.04045, c)); }
vec3 linearToSrgb(vec3 c) { c = clamp(c, 0., 1.); return mix(c * 12.92, 1.055 * pow(c, vec3(1. / 2.4)) - .055, step(.0031308, c)); }
vec3 linearToOklab(vec3 c) {
  vec3 lms = pow(max(vec3(
    .4122214708 * c.r + .5363325363 * c.g + .0514459929 * c.b,
    .2119034982 * c.r + .6806995451 * c.g + .1073969566 * c.b,
    .0883024619 * c.r + .2817188376 * c.g + .6299787005 * c.b), 0.), vec3(1. / 3.));
  return vec3(
    .2104542553 * lms.x + .7936177850 * lms.y - .0040720468 * lms.z,
    1.9779984951 * lms.x - 2.4285922050 * lms.y + .4505937099 * lms.z,
    .0259040371 * lms.x + .7827717662 * lms.y - .8086757660 * lms.z);
}
vec3 oklabToLinear(vec3 c) {
  vec3 lms = vec3(
    c.x + .3963377774 * c.y + .2158037573 * c.z,
    c.x - .1055613458 * c.y - .0638541728 * c.z,
    c.x - .0894841775 * c.y - 1.2914855480 * c.z);
  lms = lms * lms * lms;
  return vec3(
    4.0767416621 * lms.x - 3.3077115913 * lms.y + .2309699292 * lms.z,
    -1.2684380046 * lms.x + 2.6097574011 * lms.y - .3413193965 * lms.z,
    -.0041960863 * lms.x - .7034186147 * lms.y + 1.7076147010 * lms.z);
}
`;

// The liquid behind the glass, rendered small: weighted artwork colors carried
// on slow domain-warped currents, with drifting patches of the cover itself.
const fieldSource = `
precision highp float;
varying vec2 vUv;
uniform float time;
uniform vec2 aspect;
uniform vec2 pointer;
uniform vec3 labs[${BLOBS}];
uniform float weights[${BLOBS}];
uniform sampler2D artA;
uniform sampler2D artB;
uniform float artBlend;
uniform float artAmount;
${colorMath}
// 2D simplex noise (Ashima Arts, MIT).
vec3 permute(vec3 x) { return mod(((x * 34.) + 1.) * x, 289.); }
float snoise(vec2 v) {
  const vec4 C = vec4(.211324865405187, .366025403784439, -.577350269189626, .024390243902439);
  vec2 i = floor(v + dot(v, C.yy));
  vec2 x0 = v - i + dot(i, C.xx);
  vec2 i1 = x0.x > x0.y ? vec2(1., 0.) : vec2(0., 1.);
  vec4 x12 = x0.xyxy + C.xxzz;
  x12.xy -= i1;
  i = mod(i, 289.);
  vec3 p = permute(permute(i.y + vec3(0., i1.y, 1.)) + i.x + vec3(0., i1.x, 1.));
  vec3 m = max(.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.);
  m = m * m; m = m * m;
  vec3 x = 2. * fract(p * C.www) - 1.;
  vec3 h = abs(x) - .5;
  vec3 a0 = x - floor(x + .5);
  m *= 1.79284291400159 - .85373472095314 * (a0 * a0 + h * h);
  vec3 g = vec3(a0.x * x0.x + h.x * x0.y, a0.yz * x12.xz + h.yz * x12.yw);
  return 130. * dot(m, g);
}
float fbm(vec2 p) {
  float sum = 0., amp = .55;
  for (int i = 0; i < 3; i++) { sum += amp * snoise(p); p = p * 1.9 + vec2(17.1, 3.7); amp *= .45; }
  return sum;
}
void main() {
  vec2 p = (vUv - .5) * aspect + pointer * .05;
  float t = time;
  vec2 q = vec2(fbm(p * .55 + vec2(0., t * .04)), fbm(p * .55 + vec2(5.2, 1.3) - vec2(t * .035, 0.)));
  vec2 r = vec2(fbm(p * .55 + 1.5 * q + vec2(1.7, 9.2) + t * .028), fbm(p * .55 + 1.5 * q + vec2(8.3, 2.8) - t * .023));
  vec2 w = p + .55 * r;

  vec3 lab = vec3(0.);
  float total = 0.;
  for (int i = 0; i < ${BLOBS}; i++) {
    float f = float(i);
    vec2 center = vec2(sin(t * (.05 + .013 * f) + f * 2.39), cos(t * (.041 + .011 * f) + f * 1.71)) * vec2(.45 * aspect.x, .4);
    vec2 d = w - center;
    float k = weights[i] * exp(-dot(d, d) * 5.);
    lab += labs[i] * k;
    total += k;
  }
  lab /= max(total, 1e-5);

  float spin = t * .012;
  vec2 au = mat2(cos(spin), -sin(spin), sin(spin), cos(spin)) * (w * .62) + .5;
  vec3 artLab = linearToOklab(srgbToLinear(mix(texture2D(artA, au).rgb, texture2D(artB, au).rgb, artBlend)));
  // Only the cover's colorful parts drift through; its white or black areas
  // would just wash the palette out.
  float vivid = smoothstep(.03, .12, length(artLab.yz));
  float drift = smoothstep(-.2, .5, fbm(p * .5 + vec2(t * .02, 3.1)));
  lab = mix(lab, artLab, artAmount * drift * vivid * .5);

  // Smoked glass: mid tones keep their brightness, darks lift a little,
  // highlights are pressed into a band where white type stays legible. Chroma
  // is lifted most for muted colors (a dusty cover still reads as colored)
  // and only slightly for vivid ones.
  float shade = .88 + .16 * fbm(w * .7 - t * .015);
  lab.x = (lab.x < .55 ? .08 + .85 * lab.x : .5475 + .2 * (lab.x - .55)) * shade;
  lab.yz *= 1.08 + .32 * (1. - smoothstep(.05, .16, length(lab.yz)));
  gl_FragColor = vec4(linearToSrgb(oklabToLinear(lab)), 1.);
}
`;

function hexToOklab(hex: string): number[] {
  const value = /^#[0-9a-f]{6}$/i.test(hex) ? hex.slice(1) : "739caa";
  const [r, g, b] = [0, 2, 4].map((index) => {
    const c = parseInt(value.slice(index, index + 2), 16) / 255;
    return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
  });
  const l = Math.cbrt(.4122214708 * r + .5363325363 * g + .0514459929 * b);
  const m = Math.cbrt(.2119034982 * r + .6806995451 * g + .1073969566 * b);
  const s = Math.cbrt(.0883024619 * r + .2817188376 * g + .6299787005 * b);
  return [
    .2104542553 * l + .7936177850 * m - .0040720468 * s,
    1.9779984951 * l - 2.4285922050 * m + .4505937099 * s,
    .0259040371 * l + .7827717662 * m - .8086757660 * s,
  ];
}

type SceneEntry = { lab: number[]; weight: number; kind: "white" | "black" | "color" };

// Most of the scene a kind of color may take: a white cover still shows its
// colors, a dark cover stays dark and moody rather than being repainted.
const SCENE_SHARE_CAPS = { white: .3, black: .55 } as const;

function capShare(entries: SceneEntry[], kind: keyof typeof SCENE_SHARE_CAPS) {
  const cap = SCENE_SHARE_CAPS[kind];
  const total = entries.reduce((sum, entry) => sum + entry.weight, 0);
  const part = entries.reduce((sum, entry) => sum + (entry.kind === kind ? entry.weight : 0), 0);
  if (!part || part === total || part / total <= cap) return;
  const scale = (cap * (total - part)) / ((1 - cap) * part);
  entries.forEach((entry) => { if (entry.kind === kind) entry.weight *= scale; });
}

// Six weighted color bodies from the artwork's colors. Near-identical colors
// merge, and coverage is compressed so secondary colors get real area.
function sceneTargets(colors: SceneColor[]) {
  const merged: { lab: number[]; weight: number }[] = [];
  for (const color of colors) {
    if (!/^#[0-9a-f]{6}$/i.test(color.hex) || !(color.weight > 0)) continue;
    const lab = hexToOklab(color.hex);
    const twin = merged.find((entry) => Math.hypot(entry.lab[0] - lab[0], entry.lab[1] - lab[1], entry.lab[2] - lab[2]) < .07);
    if (twin) twin.weight += color.weight;
    else merged.push({ lab, weight: color.weight });
  }
  const source = merged.length ? merged : ["#5fc6bc", "#ed866f", "#697eae"].map((hex) => ({ lab: hexToOklab(hex), weight: 1 }));
  const entries: SceneEntry[] = source
    .sort((a, b) => b.weight - a.weight)
    .slice(0, BLOBS)
    .map(({ lab, weight }) => {
      const chroma = Math.hypot(lab[1], lab[2]);
      const kind = chroma < .04 && lab[0] > .7 ? "white" : chroma < .05 && lab[0] < .22 ? "black" : "color";
      return { lab, weight: weight ** .6, kind };
    });
  capShare(entries, "white");
  capShare(entries, "black");
  for (let index = 0; entries.length < BLOBS; index++) {
    const echo = entries[index % entries.length];
    entries.push({ ...echo, weight: echo.weight * .6 });
  }
  const sum = entries.reduce((value, entry) => value + entry.weight, 0) || 1;
  return { labs: entries.flatMap((entry) => entry.lab), weights: entries.map((entry) => entry.weight / sum) };
}

async function loadArtwork(url: string): Promise<HTMLCanvasElement> {
  const image = new Image();
  image.crossOrigin = "anonymous";
  image.src = url;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = ART_SIZE;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("2D canvas unavailable");
  // Pre-blur so type and fine detail never show; overscan keeps edges opaque.
  context.filter = "blur(3px)";
  context.drawImage(image, -8, -8, ART_SIZE + 16, ART_SIZE + 16);
  return canvas;
}

export function LiquidScene({ colors, artworkUrl, moving, glass }: { colors: SceneColor[]; artworkUrl: string; moving: boolean; glass: GlassStyle }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const targetRef = useRef(sceneTargets(colors));
  const movingRef = useRef(moving);
  const glassRef = useRef(glass);
  const artRef = useRef<{ image: HTMLCanvasElement | null; version: number }>({ image: null, version: 0 });
  const [available, setAvailable] = useState(true);
  const colorKey = colors.map((color) => `${color.hex}:${color.weight}`).join("|");
  movingRef.current = moving;
  glassRef.current = glass;

  useEffect(() => { targetRef.current = sceneTargets(colors); }, [colorKey]);

  useEffect(() => {
    let cancelled = false;
    if (!artworkUrl) {
      artRef.current = { image: null, version: artRef.current.version + 1 };
      return;
    }
    loadArtwork(artworkUrl)
      .then((image) => { if (!cancelled) artRef.current = { image, version: artRef.current.version + 1 }; })
      .catch(() => { if (!cancelled) artRef.current = { image: null, version: artRef.current.version + 1 }; });
    return () => { cancelled = true; };
  }, [artworkUrl]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const gl = canvas.getContext("webgl", { alpha: false, antialias: false, depth: false, powerPreference: "low-power" });
    if (!gl) { setAvailable(false); return; }
    const shaders: WebGLShader[] = [];
    const programs: WebGLProgram[] = [];
    const textures: WebGLTexture[] = [];
    let buffer: WebGLBuffer | null = null;
    let framebuffer: WebGLFramebuffer | null = null;
    let frame = 0;
    let disposed = false;
    const cleanGpu = () => {
      if (buffer) gl.deleteBuffer(buffer);
      if (framebuffer) gl.deleteFramebuffer(framebuffer);
      textures.forEach((texture) => gl.deleteTexture(texture));
      programs.forEach((program) => gl.deleteProgram(program));
      shaders.forEach((shader) => gl.deleteShader(shader));
    };
    try {
      const compile = (kind: number, source: string) => {
        const shader = gl.createShader(kind);
        if (!shader) throw new Error("Shader unavailable");
        shaders.push(shader);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) || "Shader compilation failed");
        return shader;
      };
      const vertex = compile(gl.VERTEX_SHADER, vertexSource);
      const link = (source: string, names: string[]) => {
        const program = gl.createProgram();
        if (!program) throw new Error("WebGL program unavailable");
        programs.push(program);
        gl.attachShader(program, vertex);
        gl.attachShader(program, compile(gl.FRAGMENT_SHADER, source));
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program) || "Shader linking failed");
        const uniforms = Object.fromEntries(names.map((name) => [name, gl.getUniformLocation(program, name)]));
        return { program, position: gl.getAttribLocation(program, "position"), uniforms };
      };
      const fieldPass = link(fieldSource, ["time", "aspect", "pointer", "labs", "weights", "artA", "artB", "artBlend", "artAmount"]);
      // Each glass compiles the first time it is shown. One that fails falls
      // back to frosted; frosted failing falls back to the CSS scene.
      const glassPasses = new Map<GlassStyle, ReturnType<typeof link>>();
      const glassPass = (style: GlassStyle): ReturnType<typeof link> => {
        let pass = glassPasses.get(style);
        if (!pass) {
          try {
            pass = link(glassSource(style), GLASS_UNIFORMS);
          } catch (error) {
            if (style === "frosted") throw error;
            console.warn(`The ${style} glass is unavailable; showing frosted.`, error);
            pass = glassPass("frosted");
          }
          glassPasses.set(style, pass);
        }
        return pass;
      };
      glassPass("frosted");
      glassPass(glassRef.current);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

      buffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);

      const makeTexture = (wrap: number) => {
        const texture = gl.createTexture();
        if (!texture) throw new Error("WebGL texture unavailable");
        textures.push(texture);
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, wrap);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, wrap);
        return texture;
      };
      const fieldTexture = makeTexture(gl.CLAMP_TO_EDGE);
      const artTextures = [makeTexture(gl.MIRRORED_REPEAT), makeTexture(gl.MIRRORED_REPEAT)];
      const blank = new Uint8Array(ART_SIZE * ART_SIZE * 4);
      artTextures.forEach((texture) => {
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, ART_SIZE, ART_SIZE, 0, gl.RGBA, gl.UNSIGNED_BYTE, blank);
      });
      framebuffer = gl.createFramebuffer();

      let fieldWidth = 0;
      let fieldHeight = 0;
      const resize = () => {
        const { width, height } = canvas.getBoundingClientRect();
        const ratio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(4200000 / Math.max(1, width * height)));
        canvas.width = Math.max(1, Math.round(width * ratio));
        canvas.height = Math.max(1, Math.round(height * ratio));
        fieldWidth = Math.max(64, Math.round(canvas.width / 4));
        fieldHeight = Math.max(36, Math.round(canvas.height / 4));
        gl.bindTexture(gl.TEXTURE_2D, fieldTexture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, fieldWidth, fieldHeight, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, fieldTexture, 0);
        if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("Framebuffer incomplete");
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      };
      const observer = new ResizeObserver(() => { try { resize(); } catch { setAvailable(false); } });
      observer.observe(canvas);
      resize();

      const current = { labs: [...targetRef.current.labs], weights: [...targetRef.current.weights] };
      const pointer = [0, 0];
      const target = [0, 0];
      // A new glass fades in over the one it replaces.
      let shownGlass = glassRef.current;
      let leavingGlass: GlassStyle | null = null;
      let glassFade = 1;
      let artVersion = -1;
      let artFront = 0;
      let artBlend = 0;
      let artAmount = 0;
      let clock = 40;
      let previous = 0;
      let lastDraw = 0;
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      const onPointer = (event: PointerEvent) => {
        if (event.pointerType === "touch" || reducedMotion.matches) return;
        target[0] = event.clientX / window.innerWidth - .5;
        target[1] = .5 - event.clientY / window.innerHeight;
      };
      const approach = (value: number, goal: number, rate: number) => value + (goal - value) * rate;

      const draw = (now: number) => {
        if (disposed || document.hidden) return;
        frame = requestAnimationFrame(draw);
        if (now - lastDraw < FRAME_MS - 2) return;
        const elapsed = previous ? Math.min((now - previous) / 1000, .1) : 0;
        previous = now;
        lastDraw = now;
        if (movingRef.current && !reducedMotion.matches) clock += elapsed;

        const art = artRef.current;
        if (art.version !== artVersion) {
          artVersion = art.version;
          const image = art.image;
          if (image) {
            // Nothing showing yet: load both sides so the fade starts from this
            // cover instead of the blank texture. Otherwise crossfade to it.
            const sides = artAmount < .02 ? [0, 1] : [1 - artFront];
            sides.forEach((side) => {
              gl.bindTexture(gl.TEXTURE_2D, artTextures[side]);
              gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
            });
            artFront = sides[sides.length - 1];
            if (sides.length === 2) artBlend = artFront;
          }
        }
        // A new song's colors settle in about two seconds.
        const colorRate = 1 - Math.exp(-elapsed * 1.5);
        const goal = targetRef.current;
        current.labs.forEach((value, i) => { current.labs[i] = approach(value, goal.labs[i], colorRate); });
        current.weights.forEach((value, i) => { current.weights[i] = approach(value, goal.weights[i], colorRate); });
        artBlend = approach(artBlend, artFront, 1 - Math.exp(-elapsed * 1.6));
        artAmount = approach(artAmount, art.image ? 1 : 0, 1 - Math.exp(-elapsed * .8));
        if (glassRef.current !== shownGlass) {
          // Mid-fade, whichever glass shows more stays as the one leaving.
          if (!leavingGlass || glassFade > .5) leavingGlass = shownGlass;
          shownGlass = glassRef.current;
          glassFade = reducedMotion.matches ? 1 : 0;
        }
        glassFade = Math.min(1, glassFade + elapsed / GLASS_FADE_S);
        pointer.forEach((value, i) => { pointer[i] = approach(value, target[i], .05); });

        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
        gl.viewport(0, 0, fieldWidth, fieldHeight);
        gl.useProgram(fieldPass.program);
        gl.enableVertexAttribArray(fieldPass.position);
        gl.vertexAttribPointer(fieldPass.position, 2, gl.FLOAT, false, 0, 0);
        const fu = fieldPass.uniforms;
        gl.uniform1f(fu.time, clock);
        gl.uniform2f(fu.aspect, canvas.width / canvas.height, 1);
        gl.uniform2f(fu.pointer, pointer[0], pointer[1]);
        gl.uniform3fv(fu.labs, current.labs);
        gl.uniform1fv(fu.weights, current.weights);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, artTextures[0]);
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, artTextures[1]);
        gl.uniform1i(fu.artA, 0);
        gl.uniform1i(fu.artB, 1);
        gl.uniform1f(fu.artBlend, artBlend);
        gl.uniform1f(fu.artAmount, artAmount);
        gl.drawArrays(gl.TRIANGLES, 0, 3);

        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, fieldTexture);
        const drawGlass = (style: GlassStyle, opacity: number) => {
          const pass = glassPass(style);
          gl.useProgram(pass.program);
          gl.enableVertexAttribArray(pass.position);
          gl.vertexAttribPointer(pass.position, 2, gl.FLOAT, false, 0, 0);
          gl.uniform1i(pass.uniforms.field, 0);
          gl.uniform2f(pass.uniforms.resolution, canvas.width, canvas.height);
          gl.uniform1f(pass.uniforms.time, clock);
          gl.uniform1f(pass.uniforms.opacity, opacity);
          gl.drawArrays(gl.TRIANGLES, 0, 3);
        };
        if (leavingGlass && glassFade < 1) {
          drawGlass(leavingGlass, 1);
          gl.enable(gl.BLEND);
          drawGlass(shownGlass, glassFade * glassFade * (3 - 2 * glassFade));
          gl.disable(gl.BLEND);
        } else {
          drawGlass(shownGlass, 1);
        }
      };
      const visibility = () => {
        cancelAnimationFrame(frame);
        previous = 0;
        if (!document.hidden) frame = requestAnimationFrame(draw);
      };
      const contextLost = (event: Event) => { event.preventDefault(); cancelAnimationFrame(frame); setAvailable(false); };
      window.addEventListener("pointermove", onPointer, { passive: true });
      document.addEventListener("visibilitychange", visibility);
      canvas.addEventListener("webglcontextlost", contextLost);
      frame = requestAnimationFrame(draw);
      return () => {
        disposed = true;
        cancelAnimationFrame(frame);
        observer.disconnect();
        window.removeEventListener("pointermove", onPointer);
        document.removeEventListener("visibilitychange", visibility);
        canvas.removeEventListener("webglcontextlost", contextLost);
        cleanGpu();
      };
    } catch (error) {
      console.warn("Liquid rendering unavailable; using the ambient fallback.", error);
      setAvailable(false);
      cleanGpu();
    }
  }, []);

  return <div className={`liquid-scene${available ? "" : " liquid-scene--fallback"}`} aria-hidden="true"><canvas ref={canvasRef} /></div>;
}
