// The pane in front of the liquid: one fragment shader per style, drawn at
// full resolution from the small, soft liquid texture (`field`). Every style
// keeps the colors in the same brightness band, so white type stays legible.

export const GLASS_STYLES = ["frosted", "fluted", "rain", "blocks", "stained", "pool", "crystal", "bokeh"] as const;
export type GlassStyle = (typeof GLASS_STYLES)[number];
export const glassStyle = (value: unknown): GlassStyle => (GLASS_STYLES.includes(value as GlassStyle) ? (value as GlassStyle) : "frosted");
export const GLASS_UNIFORMS = ["field", "resolution", "time", "opacity"];

const common = `
precision highp float;
varying vec2 vUv;
uniform sampler2D field;
uniform vec2 resolution;
uniform float time;
uniform float opacity;
// Hash without Sine (Dave Hoskins, MIT).
float hash(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * .1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}
vec2 hash2(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * vec3(.1031, .1030, .0973));
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.xx + p3.yz) * p3.zy);
}
float valueNoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3. - 2. * f);
  return mix(mix(hash(i), hash(i + vec2(1., 0.)), f.x), mix(hash(i + vec2(0., 1.)), hash(i + vec2(1., 1.)), f.x), f.y);
}
// Design pixels: 1080 across the screen's short side at any resolution.
float unit() { return min(resolution.x, resolution.y) / 1080.; }
// Screen-space offsets scaled so a shift reads the same across and down.
vec2 square(vec2 v) { return v * vec2(resolution.y / resolution.x, 1.); }
vec3 disperse(vec2 uv, vec2 spread) {
  return vec3(texture2D(field, uv + spread).r, texture2D(field, uv).g, texture2D(field, uv - spread).b);
}
float luma(vec3 c) { return dot(c, vec3(.2126, .7152, .0722)); }
// Shared finish: a fixed sheen in the top-left corner, a vignette, fine grain.
void finish(vec3 col, float grain) {
  col += .045 * pow(max(0., 1. - length((vUv - vec2(.05, 1.02)) * vec2(.75, 1.1))), 2.);
  col *= 1. - .26 * smoothstep(.3, .95, length((vUv - .5) * vec2(1.05, 1.2)) * 1.3);
  col += (hash(gl_FragCoord.xy) - .5) * grain;
  gl_FragColor = vec4(col, opacity);
}
`;

// Border distance (x) and cell id (yz) of a jittered grid. The second pass
// finds the exact distance to the nearest border.
const cells = `
vec3 cells(vec2 x) {
  vec2 n = floor(x), f = fract(x);
  vec2 nearest = vec2(0.), owner = vec2(0.);
  float best = 8.;
  for (int j = -1; j <= 1; j++) for (int i = -1; i <= 1; i++) {
    vec2 g = vec2(float(i), float(j));
    vec2 r = g + .15 + .7 * hash2(n + g) - f;
    float d = dot(r, r);
    if (d < best) { best = d; nearest = r; owner = g; }
  }
  float border = 8.;
  for (int j = -2; j <= 2; j++) for (int i = -2; i <= 2; i++) {
    vec2 g = owner + vec2(float(i), float(j));
    vec2 r = g + .15 + .7 * hash2(n + g) - f;
    vec2 between = r - nearest;
    if (dot(between, between) > 1e-5) border = min(border, dot(.5 * (nearest + r), normalize(between)));
  }
  return vec3(border, n + owner);
}
vec2 cellPoint(vec2 id) { return id + .15 + .7 * hash2(id); }
`;

const sources: Record<GlassStyle, string> = {
  // A faint hammered ripple over an even blur.
  frosted: `
void main() {
  vec2 px = gl_FragCoord.xy;
  vec2 ripple = (vec2(valueNoise(px / 170.), valueNoise(px / 170. + 31.7)) - .5) * .018;
  finish(texture2D(field, vUv + ripple).rgb, .034);
}`,

  // Each rib is a shallow cylindrical lens: it shows a squeezed, mirrored
  // view of what is behind it, so colors break into soft vertical slices.
  fluted: `
void main() {
  vec2 px = gl_FragCoord.xy;
  float rib = max(18., resolution.y / 21.);
  float f = fract(px.x / rib);
  float s = f * 2. - 1.;
  vec2 bend = vec2(-s * rib * 1.1 / resolution.x, 0.);
  vec3 col = disperse(vUv + bend, bend * .06);
  float crease = smoothstep(0., .07, f) * smoothstep(1., .93, f);
  float lit = 1. - .04 * s + .045 * exp(-pow((f - .28) * 8., 2.));
  finish(col * lit * (.93 + .07 * crease), .024);
}`,

  // Rain on the window: beads land and slowly evaporate, and bigger drops
  // slide down wavering lanes, clearing the mist and leaving small beads
  // behind. Every drop is a tiny lens: a flipped, brighter view of the colors
  // with a dark rim and a highlight.
  rain: `
const float LANE = 96.;
float lanePath(float y, float seed) {
  return .5 + .19 * sin(y * .011 + seed * 31.) + .07 * sin(y * .037 + seed * 17.);
}
// A drop as (normal.xy, coverage), anti-aliased over about a pixel.
vec3 drop(vec2 d, float radius, float scale) {
  if (radius < .4) return vec3(0.);
  vec2 n = d / radius;
  float aa = min(.9, 1.4 / (radius * scale));
  return vec3(n, 1. - smoothstep(1. - aa, 1., length(n)));
}
vec3 stronger(vec3 a, vec3 b) { return b.z > a.z ? b : a; }
// At most one bead per grid cell, each on its own land-and-dry cycle.
vec3 beads(vec2 q, float size, vec2 radii, float density, float life, float scale) {
  vec2 cell = floor(q / size);
  if (hash(cell + 11.7) > density) return vec3(0.);
  float phase = fract(time / life + hash(cell + 3.9));
  float radius = mix(radii.x, radii.y, pow(hash(cell + 7.3), 2.)) * smoothstep(0., .02, phase) * (1. - smoothstep(.6, 1., phase));
  vec2 center = (cell + .5 + (hash2(cell) - .5) * (1. - 2. * radii.y / size)) * size;
  return drop(q - center, radius, scale);
}
void main() {
  float scale = unit();
  vec2 q = gl_FragCoord.xy / scale;

  // The lane under this pixel and the drops sliding down it, stalling and
  // lurching like real ones.
  float column = floor(q.x / LANE);
  float x = q.x - column * LANE;
  float seed = hash(vec2(column, 4.7));
  float speed = 16. + 30. * hash(vec2(column, 9.1));
  float spacing = 560. + 640. * hash(vec2(column, 1.3));
  float radius = 11. + 7. * hash(vec2(column, 5.9));
  float trailLength = 160. + 240. * hash(vec2(column, 2.2));
  float along = q.y + speed * time + speed * 1.1 * sin(time * .7 + seed * 40.) + seed * spacing;
  float slot = floor(along / spacing);
  float present = step(.3, hash(vec2(column, slot))) * step(.25, seed);
  float rise = along - slot * spacing - radius * 2.;
  vec2 d = vec2(x - lanePath(q.y - rise, seed) * LANE, rise);
  d.y *= d.y > 0. ? .72 : 1.;
  vec3 slider = drop(d, radius * present, scale);
  float fade = present * step(0., rise) * max(0., 1. - rise / trailLength);
  float trail = fade * (1. - smoothstep(radius * .4, radius * .95, abs(x - lanePath(q.y, seed) * LANE)));

  // Small beads left in the trail stay where they fell.
  vec2 cell = floor(q / 10.);
  vec2 center = (cell + .5 + (hash2(cell + 41.) - .5) * .4) * 10.;
  float riseThere = rise + center.y - q.y;
  float wetThere = present * step(0., riseThere) * max(0., 1. - riseThere / trailLength)
    * (1. - smoothstep(radius * .2, radius * .6, abs(center.x - column * LANE - lanePath(center.y, seed) * LANE)));
  float beadRadius = (1.3 + 2.4 * hash(cell + 9.)) * step(.45, hash(cell + 2.)) * smoothstep(.15, .5, wetThere);
  vec3 left = drop(q - center, beadRadius, scale);

  // Beads that landed on the glass; a passing drop sweeps them away.
  vec3 landed = stronger(beads(q, 46., vec2(4., 12.), .42, 41., scale), beads(q + 13., 22., vec2(1.5, 5.), .55, 27., scale));
  landed.z *= 1. - trail;
  vec3 top = stronger(stronger(landed, left), slider);

  vec3 behind = texture2D(field, vUv).rgb;
  vec3 mist = mix(behind, vec3(luma(behind)), .2) * .9 + .04;
  vec3 pane = mix(mist, behind * 1.03, trail);
  vec2 n = top.xy;
  vec3 seen = texture2D(field, vUv - square(n) * .07).rgb * 1.12 + .015;
  seen *= 1. - .55 * smoothstep(.45, 1., length(n));
  seen += .5 * (1. - smoothstep(.04, .3, length(n - vec2(-.3, .38))));
  seen += .12 * (1. - smoothstep(0., .6, length(n - vec2(.18, -.5)))) * seen;
  finish(mix(pane, seen, top.z), .03);
}`,

  // Glass blocks: a wall of pillowed bricks. Each block is a lens showing a
  // shrunken, flipped view of the colors through a rippled face; the rim
  // catches the light and the mortar joints stay dark.
  blocks: `
void main() {
  float scale = unit();
  float size = min(resolution.x, resolution.y) / 5.;
  vec2 p = (gl_FragCoord.xy - resolution * .5) / size + .5;
  vec2 f = fract(p) * 2. - 1.;
  vec2 a = abs(f);
  float edge = max(a.x, a.y);
  vec2 rim = sign(f) * pow(a, vec2(6.));
  // Rings pressed into the face, like the waves cast into real glass block.
  float ripple = sin(length(f) * 16.) * (1. - smoothstep(.6, .9, edge));
  vec2 bend = (-f * 1.1 - rim * .5 + normalize(f + 1e-4) * ripple * .07) * size / resolution;
  vec3 col = disperse(vUv + bend, bend * .1);
  // Light from the top left: the rim facing it brightens, the far rim shades.
  vec2 side = a.x > a.y ? vec2(sign(f.x), 0.) : vec2(0., sign(f.y));
  float bevel = smoothstep(.72, .97, edge);
  col *= 1. + bevel * (.18 * dot(side, vec2(-.6, .8)) + .04);
  // A thin line of light where the face meets the rim, and a soft gloss.
  col *= 1. + .12 * exp(-pow((edge - .8) * 28., 2.));
  col += .05 * (1. - smoothstep(0., .75, length(f - vec2(-.4, .45))));
  float fromJoint = (1. - edge) * size * .5;
  col *= .86 + .14 * smoothstep(0., 22. * scale, fromJoint);
  float joint = 1. - smoothstep(2.6 * scale, 3.6 * scale, fromJoint);
  finish(mix(col, col * .2 + .012, joint), .026);
}`,

  // Stained glass: leaded panes lit from behind. Each pane takes one of the
  // colors flowing behind it, its glass a little uneven, with dark lead between.
  stained: `${cells}
void main() {
  float scale = unit();
  float size = min(resolution.x, resolution.y) / 6.;
  vec2 p = gl_FragCoord.xy / size;
  // Gently curved leads, as if cut by hand.
  p += (vec2(valueNoise(p * 1.4), valueNoise(p * 1.4 + 17.)) - .5) * .3;
  vec3 c = cells(p);
  vec2 id = c.yz;
  float tone = hash(id + 5.3);
  vec2 source = clamp(cellPoint(id) * size / resolution + (hash2(id + 2.1) - .5) * .5, .02, .98);
  vec3 pane = mix(texture2D(field, vUv).rgb, texture2D(field, source).rgb, .75);
  pane = max(vec3(0.), mix(vec3(luma(pane)), pane, 1.3)) * (.78 + .36 * tone);
  // Streaky, hand-rolled glass, turned a different way in every pane.
  float angle = tone * 6.2831;
  vec2 s = mat2(cos(angle), -sin(angle), sin(angle), cos(angle)) * (gl_FragCoord.xy / size);
  pane *= .93 + .12 * valueNoise(s * vec2(10., 1.8) + tone * 40.);
  float border = c.x * size;
  float came = 3.4 * scale;
  pane *= .8 + .2 * smoothstep(came, came + 16. * scale, border);
  vec3 lead = vec3(.022, .021, .026) + .045 * (1. - smoothstep(0., came, border));
  finish(mix(pane, lead, 1. - smoothstep(came - scale, came, border)), .028);
}`,

  // Looking down into a pool: slow swells bend the colors, and the light
  // they focus plays across them as caustics.
  pool: `
// Bright where two moving cells meet, a net like light on a pool floor: a
// soft glow around a thin, bright core.
float caustic(vec2 p, float t) {
  vec2 n = floor(p), f = fract(p);
  float f1 = 8., f2 = 8.;
  for (int j = -1; j <= 1; j++) for (int i = -1; i <= 1; i++) {
    vec2 g = vec2(float(i), float(j));
    vec2 h = hash2(n + g);
    vec2 o = .5 + .4 * sin(t * (.35 + .25 * h.yx) + 6.2831 * h);
    float d = length(g + o - f);
    if (d < f1) { f2 = f1; f1 = d; } else { f2 = min(f2, d); }
  }
  float e = f2 - f1;
  return exp(-e * 12.) * .55 + exp(-e * 45.) * .6;
}
vec2 swell(vec2 p, float t) {
  return vec2(.8, .6) * cos(dot(p, vec2(.8, .6)) * 1.1 + t * .8)
    + vec2(-.6, .8) * cos(dot(p, vec2(-.6, .8)) * 1.7 - t * .9) * .6
    + vec2(.28, -.96) * cos(dot(p, vec2(.28, -.96)) * 2.3 + t * 1.1) * .4
    + vec2(-.96, -.28) * cos(dot(p, vec2(-.96, -.28)) * 3.1 - t * 1.3) * .25;
}
void main() {
  vec2 q = gl_FragCoord.xy / unit();
  float t = time;
  vec2 p = q / 140.;
  vec2 slope = swell(p, t * .7) + (vec2(valueNoise(p * 2. + t * .15), valueNoise(p * 2. - t * .12 + 9.)) - .5) * 1.4;
  vec2 bend = square(slope) * .006;
  vec3 col = disperse(vUv + bend, bend * .2);
  // The net splits slightly into colors, and the sun dapples it in patches.
  vec2 cp = q / 230. + slope * .09;
  vec2 split = slope * .012 + .008;
  vec3 light = vec3(caustic(cp + split, t * .5), caustic(cp, t * .5), caustic(cp - split, t * .5)) * .8
    + caustic(cp * 1.7 + 3.7, t * .65) * .2;
  light *= .5 + .5 * smoothstep(.2, .75, valueNoise(q / 480. + t * .04));
  finish(col * (.92 + .5 * light) + light * .02, .026);
}`,

  // Cut crystal: irregular facets, each tilted its own way, so every facet
  // shows a shifted view of the colors with a prism's color fringe, and
  // glints catch along the edges where facets meet.
  crystal: `${cells}
void main() {
  float scale = unit();
  float size = min(resolution.x, resolution.y) / 4.2;
  vec3 c = cells(gl_FragCoord.xy / size);
  vec2 tilt = hash2(c.yz + 3.1) * 2. - 1.;
  vec2 shift = square(tilt) * .07;
  vec3 col = disperse(vUv + shift, shift * .45);
  col *= .9 + .2 * dot(tilt, vec2(-.6, .8));
  float border = c.x * size;
  col *= .9 + .1 * smoothstep(0., 18. * scale, border);
  float sparkle = smoothstep(.55, .9, valueNoise(gl_FragCoord.xy / (60. * scale) + time * .03));
  float glint = 1. - smoothstep(.6 * scale, 2. * scale, border);
  finish(col + glint * (.05 + .25 * sparkle) * (.6 + col), .026);
}`,

  // Out of focus: the colors sit far behind the lens while soft discs of
  // light drift in front, each tinted by the color behind it.
  bokeh: `
vec3 discs(vec2 q, float size, float soft, float density, vec2 drift, float seed) {
  vec2 p = q - drift * time;
  vec2 cell = floor(p / size);
  vec3 sum = vec3(0.);
  for (int j = -1; j <= 1; j++) for (int i = -1; i <= 1; i++) {
    vec2 c = cell + vec2(float(i), float(j));
    if (hash(c + seed) > density) continue;
    vec2 center = (c + hash2(c + seed + 1.7)) * size;
    float dist = length(p - center) / (size * (.3 + .4 * hash(c + seed + 5.1)));
    if (dist > 1.) continue;
    vec3 tint = texture2D(field, clamp((center + drift * time) * unit() / resolution, 0., 1.)).rgb;
    float pulse = .7 + .3 * sin(time * (.25 + .3 * hash(c + seed + 8.)) + hash(c + seed + 2.) * 40.);
    sum += tint * (1. - smoothstep(1. - soft, 1., dist)) * (.6 + .4 * smoothstep(.5, 1., dist)) * pulse;
  }
  return sum;
}
void main() {
  vec2 q = gl_FragCoord.xy / unit();
  vec3 col = texture2D(field, vUv).rgb * .82;
  vec3 light = discs(q, 300., .5, .16, vec2(4., 7.), 0.) * .38
    + discs(q, 150., .14, .15, vec2(-6., 11.), 31.) * .5
    + discs(q, 72., .12, .13, vec2(3., 16.), 77.) * .6;
  // Calmer behind the cover and the type in the middle.
  light *= .5 + .5 * smoothstep(.12, .42, length(square(vUv - .5) * vec2(1., 1.2)));
  // Screen blend: overlapping light adds up without blowing out.
  finish(1. - (1. - col) * (1. - min(light * 1.35, vec3(.85))), .03);
}`,
};

export const glassSource = (style: GlassStyle) => common + sources[style];
