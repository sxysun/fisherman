import "./styles.css";

const canvas = document.querySelector<HTMLCanvasElement>("#signal-canvas");
const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

type Trace = {
  x: number;
  y: number;
  length: number;
  speed: number;
  hue: number;
  width: number;
};

const traces: Trace[] = [];
const palette = [42, 145, 194, 11];
let frame = 0;

function resizeCanvas(ctx: CanvasRenderingContext2D) {
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const width = window.innerWidth;
  const height = window.innerHeight;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function seedTraces() {
  traces.length = 0;
  const count = Math.max(28, Math.floor(window.innerWidth / 42));
  for (let i = 0; i < count; i += 1) {
    traces.push({
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      length: 80 + Math.random() * 240,
      speed: 0.22 + Math.random() * 0.72,
      hue: palette[i % palette.length],
      width: 1 + Math.random() * 2.2
    });
  }
}

function drawGrid(ctx: CanvasRenderingContext2D, width: number, height: number) {
  ctx.save();
  ctx.strokeStyle = "rgba(238, 231, 213, 0.055)";
  ctx.lineWidth = 1;
  const step = 48;
  for (let x = (frame * 0.05) % step; x < width; x += step) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
  for (let y = 0; y < height; y += step) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }
  ctx.restore();
}

function draw(ctx: CanvasRenderingContext2D) {
  if (!canvas) return;
  const width = window.innerWidth;
  const height = window.innerHeight;
  ctx.clearRect(0, 0, width, height);

  const bg = ctx.createLinearGradient(0, 0, width, height);
  bg.addColorStop(0, "#10120f");
  bg.addColorStop(0.48, "#1a1b17");
  bg.addColorStop(1, "#0c1111");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, width, height);
  drawGrid(ctx, width, height);

  for (const trace of traces) {
    trace.y += prefersReducedMotion ? 0 : trace.speed;
    if (trace.y - trace.length > height) {
      trace.y = -trace.length;
      trace.x = Math.random() * width;
    }

    const gradient = ctx.createLinearGradient(trace.x, trace.y - trace.length, trace.x, trace.y);
    gradient.addColorStop(0, `hsla(${trace.hue}, 88%, 64%, 0)`);
    gradient.addColorStop(0.65, `hsla(${trace.hue}, 88%, 64%, 0.18)`);
    gradient.addColorStop(1, `hsla(${trace.hue}, 88%, 64%, 0.72)`);
    ctx.strokeStyle = gradient;
    ctx.lineWidth = trace.width;
    ctx.beginPath();
    ctx.moveTo(trace.x, trace.y - trace.length);
    ctx.lineTo(trace.x, trace.y);
    ctx.stroke();
  }

  frame += 1;
  if (!prefersReducedMotion) requestAnimationFrame(() => draw(ctx));
}

if (canvas) {
  const ctx = canvas.getContext("2d");
  if (ctx) {
    resizeCanvas(ctx);
    seedTraces();
    draw(ctx);
    window.addEventListener("resize", () => {
      resizeCanvas(ctx);
      seedTraces();
    });
  }
}
