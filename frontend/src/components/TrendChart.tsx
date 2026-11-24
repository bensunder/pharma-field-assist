import type { ChartData } from "../lib/types";

const PALETTE = ["#2563eb", "#0d9488", "#d97706", "#9333ea", "#dc2626", "#4b5563"];

export function TrendChart({ chart }: { chart: ChartData }) {
  const W = 420;
  const H = 150;
  const PAD = { l: 40, r: 10, t: 10, b: 22 };
  const entries = Object.entries(chart.series);
  const all = entries.flatMap(([, v]) => v);
  const max = Math.max(...all);
  const min = Math.min(...all);
  const span = max - min || 1;
  const x = (i: number) => PAD.l + (i * (W - PAD.l - PAD.r)) / Math.max(chart.weeks.length - 1, 1);
  const y = (v: number) => PAD.t + (1 - (v - min) / span) * (H - PAD.t - PAD.b);
  const fmt = (v: number) => (chart.metric === "market_share" ? `${(v * 100).toFixed(0)}%` : Math.round(v).toLocaleString());

  return (
    <figure className="chart" aria-label={`${chart.metric} trend`}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img">
        {[min, (min + max) / 2, max].map((v) => (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} className="grid" />
            <text x={PAD.l - 6} y={y(v) + 3} textAnchor="end" className="axis">
              {fmt(v)}
            </text>
          </g>
        ))}
        {chart.weeks.map((w, i) =>
          i % Math.ceil(chart.weeks.length / 4) === 0 || i === chart.weeks.length - 1 ? (
            <text key={w} x={x(i)} y={H - 6} textAnchor="middle" className="axis">
              {w.slice(5)}
            </text>
          ) : null,
        )}
        {entries.map(([name, values], si) => (
          <polyline
            key={name}
            fill="none"
            stroke={PALETTE[si % PALETTE.length]}
            strokeWidth={2}
            points={values.map((v, i) => `${x(i)},${y(v)}`).join(" ")}
          >
            <title>{name}</title>
          </polyline>
        ))}
      </svg>
      {entries.length > 1 && (
        <figcaption className="legend">
          {entries.map(([name], si) => (
            <span key={name}>
              <i style={{ background: PALETTE[si % PALETTE.length] }} />
              {name}
            </span>
          ))}
        </figcaption>
      )}
    </figure>
  );
}
