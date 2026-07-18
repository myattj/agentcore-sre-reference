"use client";

import { useId } from "react";
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  PieChart,
  Pie,
  AreaChart,
  Area,
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Cell,
} from "recharts";
import type {
  Panel,
  ChartPanel,
  TablePanel,
  StatPanel,
  TextPanel,
  ListPanel,
} from "./page";

// Visual palette for chart datasets. Every chart also has a screen-reader
// data table below it, so meaning never depends on colour alone.
const COLORS = [
  "#6b46c1", // accent purple
  "#2563eb", // blue
  "#059669", // emerald
  "#d97706", // amber
  "#dc2626", // red
  "#7c3aed", // violet
  "#0891b2", // cyan
  "#c026d3", // fuchsia
];

function PanelCard({
  title,
  ariaLabel,
  children,
}: {
  title?: string;
  ariaLabel: string;
  children: React.ReactNode;
}) {
  const headingId = useId();
  return (
    <section
      aria-label={title ? undefined : ariaLabel}
      aria-labelledby={title ? headingId : undefined}
      className="min-w-0 rounded-lg border border-[color:var(--border)] bg-white p-5 shadow-sm"
    >
      {title && (
        <h2 id={headingId} className="mb-4 text-base font-medium text-[color:var(--foreground)]">
          {title}
        </h2>
      )}
      {children}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Chart panel
// ---------------------------------------------------------------------------

function ChartPanelView({ panel }: { panel: ChartPanel }) {
  const series = panel.datasets.map((dataset, index) => ({
    key: `series_${index}`,
    label: dataset.label,
  }));
  // Reshape data for Recharts: [{name, dataset1, dataset2, ...}, ...]
  const data = panel.labels.map((label, i) => {
    const point: Record<string, string | number> = { name: label };
    panel.datasets.forEach((dataset, index) => {
      point[series[index].key] = dataset.data[i] ?? 0;
    });
    return point;
  });

  return (
    <PanelCard title={panel.title} ariaLabel={`${panel.chart_type} chart`}>
      <div aria-hidden="true">
        <ResponsiveContainer width="100%" height={320}>
          {renderChart(panel.chart_type, data, series, panel)}
        </ResponsiveContainer>
      </div>
      <table className="sr-only">
        <caption>{panel.title ?? `${panel.chart_type} chart data`}</caption>
        <thead>
          <tr>
            <th scope="col">Label</th>
            {series.map((item) => <th key={item.key} scope="col">{item.label}</th>)}
          </tr>
        </thead>
        <tbody>
          {panel.labels.map((label, rowIndex) => (
            <tr key={`${label}-${rowIndex}`}>
              <th scope="row">{label}</th>
              {panel.datasets.map((dataset, seriesIndex) => (
                <td key={series[seriesIndex].key}>{dataset.data[rowIndex]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </PanelCard>
  );
}

function renderChart(
  chartType: ChartPanel["chart_type"],
  data: Record<string, string | number>[],
  series: { key: string; label: string }[],
  panel: ChartPanel,
) {
  const axisProps = {
    xAxis: (
      <XAxis
        dataKey="name"
        tick={{ fontSize: 12 }}
        label={
          panel.x_label
            ? { value: panel.x_label, position: "insideBottom", offset: -4, fontSize: 12 }
            : undefined
        }
      />
    ),
    yAxis: (
      <YAxis
        tick={{ fontSize: 12 }}
        label={
          panel.y_label
            ? { value: panel.y_label, angle: -90, position: "insideLeft", fontSize: 12 }
            : undefined
        }
      />
    ),
  };

  switch (chartType) {
    case "bar":
      return (
        <BarChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          {axisProps.xAxis}
          {axisProps.yAxis}
          <Tooltip />
          <Legend />
          {series.map((item, i) => (
            <Bar key={item.key} dataKey={item.key} name={item.label} fill={COLORS[i % COLORS.length]} />
          ))}
        </BarChart>
      );

    case "area":
      return (
        <AreaChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          {axisProps.xAxis}
          {axisProps.yAxis}
          <Tooltip />
          <Legend />
          {series.map((item, i) => (
            <Area
              key={item.key}
              type="monotone"
              dataKey={item.key}
              name={item.label}
              stroke={COLORS[i % COLORS.length]}
              fill={COLORS[i % COLORS.length]}
              fillOpacity={0.15}
            />
          ))}
        </AreaChart>
      );

    case "pie": {
      // For pie charts, use the first dataset's data with labels as names
      const pieData = panel.labels.map((label, i) => ({
        name: label,
        value: panel.datasets[0]?.data[i] ?? 0,
      }));
      return (
        <PieChart>
          <Tooltip />
          <Legend />
          <Pie
            data={pieData}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy="50%"
            outerRadius={120}
            label
          >
            {pieData.map((_, i) => (
              <Cell key={i} fill={COLORS[i % COLORS.length]} />
            ))}
          </Pie>
        </PieChart>
      );
    }

    case "scatter":
      return (
        <ScatterChart>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis
            type="number"
            dataKey="x"
            domain={[-0.5, Math.max(panel.labels.length - 0.5, 0.5)]}
            allowDecimals={false}
            tickFormatter={(value) => panel.labels[Math.round(Number(value))] ?? ""}
            tick={{ fontSize: 12 }}
            label={
              panel.x_label
                ? { value: panel.x_label, position: "insideBottom", offset: -4, fontSize: 12 }
                : undefined
            }
          />
          <YAxis
            type="number"
            dataKey="y"
            tick={{ fontSize: 12 }}
            label={
              panel.y_label
                ? { value: panel.y_label, angle: -90, position: "insideLeft", fontSize: 12 }
                : undefined
            }
          />
          <Tooltip />
          <Legend />
          {series.map((item, i) => (
            <Scatter
              key={item.key}
              name={item.label}
              data={panel.labels.map((label, pointIndex) => ({
                x: pointIndex,
                y: panel.datasets[i].data[pointIndex],
                label,
              }))}
              fill={COLORS[i % COLORS.length]}
            />
          ))}
        </ScatterChart>
      );

    case "line":
    default:
      return (
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          {axisProps.xAxis}
          {axisProps.yAxis}
          <Tooltip />
          <Legend />
          {series.map((item, i) => (
            <Line
              key={item.key}
              type="monotone"
              dataKey={item.key}
              name={item.label}
              stroke={COLORS[i % COLORS.length]}
              dot={false}
              strokeWidth={2}
            />
          ))}
        </LineChart>
      );
  }
}

// ---------------------------------------------------------------------------
// Table panel
// ---------------------------------------------------------------------------

function TablePanelView({ panel }: { panel: TablePanel }) {
  return (
    <PanelCard title={panel.title} ariaLabel="Data table">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{panel.title ?? "Dashboard data table"}</caption>
          <thead>
            <tr className="border-b border-[color:var(--border)]">
              {panel.columns.map((col) => (
                <th
                  key={col}
                  scope="col"
                  className="whitespace-nowrap px-3 py-2 font-medium text-[color:var(--muted)]"
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {panel.rows.map((row, i) => (
              <tr
                key={i}
                className="border-b border-[color:var(--border)] last:border-0"
              >
                {row.map((cell, j) => (
                  <td
                    key={j}
                    className="whitespace-nowrap px-3 py-2 text-[color:var(--foreground)]"
                  >
                    {cell === null ? "\u2014" : String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PanelCard>
  );
}

// ---------------------------------------------------------------------------
// Stat panel
// ---------------------------------------------------------------------------

function StatPanelView({ panel }: { panel: StatPanel }) {
  const trendArrow =
    panel.trend === "up" ? "\u2191" : panel.trend === "down" ? "\u2193" : "";

  return (
    <PanelCard title={panel.title} ariaLabel="Key metric">
      <div className="flex items-baseline gap-3">
        <span className="text-3xl font-bold text-[color:var(--foreground)]">
          {panel.value}
        </span>
        {panel.delta && (
          <span
            aria-label={`${panel.trend ?? "flat"} trend: ${panel.delta}`}
            className="text-sm font-medium text-[color:var(--muted)]"
          >
            <span aria-hidden="true">{trendArrow} </span>{panel.delta}
          </span>
        )}
      </div>
    </PanelCard>
  );
}

// ---------------------------------------------------------------------------
// Text panel
// ---------------------------------------------------------------------------

function TextPanelView({ panel }: { panel: TextPanel }) {
  return (
    <PanelCard title={panel.title} ariaLabel="Dashboard note">
      <div className="break-words whitespace-pre-wrap text-sm leading-relaxed text-[color:var(--foreground)] [overflow-wrap:anywhere]">
        {panel.content}
      </div>
    </PanelCard>
  );
}

// ---------------------------------------------------------------------------
// List panel
// ---------------------------------------------------------------------------

function ListPanelView({ panel }: { panel: ListPanel }) {
  return (
    <PanelCard title={panel.title} ariaLabel="Dashboard list">
      <dl className="space-y-2">
        {panel.items.map((item) => (
          <div key={item.key} className="flex min-w-0 gap-2 text-sm">
            <dt className="min-w-[120px] shrink-0 font-medium text-[color:var(--muted)]">
              {item.key}
            </dt>
            <dd className="min-w-0 break-words text-[color:var(--foreground)] [overflow-wrap:anywhere]">
              {item.value}
            </dd>
          </div>
        ))}
      </dl>
    </PanelCard>
  );
}

// ---------------------------------------------------------------------------
// Renderer — lays out all panels in a responsive grid
// ---------------------------------------------------------------------------

export function DashboardRenderer({ panels }: { panels: Panel[] }) {
  if (panels.length === 0) {
    return <p className="text-sm text-[color:var(--muted)]">This dashboard has no panels.</p>;
  }
  return (
    <div className="grid min-w-0 gap-6 sm:grid-cols-2 lg:grid-cols-3">
      {panels.map((panel, i) => {
        // Charts and tables span full width; stats/text/lists fit in grid
        const isWide = panel.type === "chart" || panel.type === "table";
        return (
          <div
            key={i}
            className={`min-w-0 ${isWide ? "sm:col-span-2 lg:col-span-3" : ""}`}
          >
            <PanelRenderer panel={panel} />
          </div>
        );
      })}
    </div>
  );
}

function PanelRenderer({ panel }: { panel: Panel }) {
  switch (panel.type) {
    case "chart":
      return <ChartPanelView panel={panel} />;
    case "table":
      return <TablePanelView panel={panel} />;
    case "stat":
      return <StatPanelView panel={panel} />;
    case "text":
      return <TextPanelView panel={panel} />;
    case "list":
      return <ListPanelView panel={panel} />;
    default:
      return (
        <PanelCard title="Unsupported panel" ariaLabel="Unsupported panel">
          <p className="text-sm text-[color:var(--muted)]">
            This panel cannot be displayed.
          </p>
        </PanelCard>
      );
  }
}
