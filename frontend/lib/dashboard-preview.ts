export type DashboardProvider = {
  key: string;
  name: string;
  category: string;
  description: string;
  auth_type: string;
  setup_state: string;
  connection_count: number;
};

export type DashboardPreviewMetric = {
  label: string;
  value: string;
  period: string;
};

export const PREVIEW_WORKSPACE = {
  name: "Orbit Preview Workspace",
} as const;

export const PREVIEW_PROVIDERS: DashboardProvider[] = [
  {
    key: "google_analytics",
    name: "Google Analytics 4",
    category: "Analytics",
    description: "Explore website and app performance metrics.",
    auth_type: "OAuth 2.0",
    setup_state: "ready",
    connection_count: 0,
  },
  {
    key: "google_ads",
    name: "Google Ads",
    category: "Advertising",
    description: "Review campaign performance and advertising metrics.",
    auth_type: "OAuth 2.0",
    setup_state: "ready",
    connection_count: 0,
  },
  {
    key: "meta_ads",
    name: "Meta Ads",
    category: "Advertising",
    description: "Explore campaign insights from Meta advertising.",
    auth_type: "OAuth 2.0",
    setup_state: "ready",
    connection_count: 0,
  },
  {
    key: "supermetrics",
    name: "Supermetrics",
    category: "Marketing data",
    description: "Bring marketing data sources into one workspace.",
    auth_type: "API key",
    setup_state: "ready",
    connection_count: 0,
  },
  {
    key: "groq",
    name: "Groq",
    category: "AI",
    description: "Configure AI-assisted workflows for your data.",
    auth_type: "API key",
    setup_state: "ready",
    connection_count: 0,
  },
];

export const PREVIEW_METRICS: DashboardPreviewMetric[] = [
  { label: "Sample sessions", value: "18,420", period: "Illustrative · last 30 days" },
  { label: "Sample active users", value: "12,840", period: "Illustrative · last 30 days" },
  { label: "Sample ad spend", value: "$8,412.50", period: "Illustrative · last 30 days" },
  { label: "Sample conversions", value: "684", period: "Illustrative · last 30 days" },
];

export function isDashboardPreviewMode(): boolean {
  return process.env.NEXT_PUBLIC_DASHBOARD_PREVIEW_MODE === "true";
}
