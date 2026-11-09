"use client";
import { useEffect, useState } from "react";
import {
  Activity,
  ArrowUpRight,
  Bell,
  Check,
  CircleHelp,
  Cloud,
  Database,
  LayoutDashboard,
  Link2,
  LogOut,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { apiFetch as authenticatedApiFetch } from "@/lib/api";
import { useAuth } from "@/contexts/AuthProvider";
import {
  PREVIEW_METRICS,
  PREVIEW_PROVIDERS,
  type DashboardProvider,
} from "@/lib/dashboard-preview";

type Provider = DashboardProvider;
type Connection = {
  id: string;
  provider_key: string;
  display_name: string;
  status: string;
  connection_type: string;
  has_secret?: boolean;
  property_id?: string | null;
  property_name?: string | null;
  account_name?: string | null;
  customer_id?: string | null;
  customer_name?: string | null;
  login_customer_id?: string | null;
  ad_account_id?: string | null;
  ad_account_name?: string | null;
  currency?: string | null;
  ds_id?: string | null;
  ds_name?: string | null;
  ds_accounts?: string | null;
  selected_model?: string | null;
};
type SupermetricsSource = {
  ds_id: string;
  name: string;
  category?: string | null;
  requires_accounts: boolean;
};
type SupermetricsAccount = {
  account_id: string;
  account_name: string;
};
type SupermetricsReportRow = {
  id: string;
  ds_id: string;
  report_date: string;
  campaign_id?: string | null;
  campaign_name?: string | null;
  channel_group?: string | null;
  impressions: number;
  clicks: number;
  cost: number | string;
  conversions: number;
  conversions_value?: number | string | null;
};
type SupermetricsReportSummary = {
  total_impressions: number;
  total_clicks: number;
  total_cost: number | string;
  total_conversions: number;
  total_conversions_value?: number | string | null;
};
type GroqModelItem = {
  id: string;
  name: string;
  description?: string | null;
  owned_by?: string | null;
  context_window?: number;
  active?: boolean;
};
type GroqCompletionResult = {
  model: string;
  content: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  finish_reason: string;
};
type MetaAdsAccount = {
  account_id: string;
  name: string;
  currency?: string | null;
  account_status?: number | null;
};
type MetaAdsCampaignReportRow = {
  id: string;
  ad_account_id: string;
  report_date: string;
  campaign_id: string;
  campaign_name: string;
  objective?: string | null;
  impressions: number;
  reach: number;
  clicks: number;
  spend: number | string;
  conversions: number;
  conversions_value?: number | string | null;
  ctr?: number | string | null;
  cpc?: number | string | null;
  cpm?: number | string | null;
  frequency?: number | string | null;
};
type MetaAdsReportSummary = {
  currency?: string | null;
  total_impressions: number;
  total_reach: number;
  total_clicks: number;
  total_spend: number | string;
  total_conversions: number;
  total_conversions_value?: number | string | null;
  average_ctr?: number | string | null;
  average_cpc?: number | string | null;
  average_cpm?: number | string | null;
  average_frequency?: number | string | null;
};
type GoogleAdsCustomer = {
  customer_id: string;
  descriptive_name: string;
  is_manager: boolean;
  currency_code?: string | null;
  time_zone?: string | null;
  login_customer_id?: string | null;
  test_account?: boolean;
};
type GoogleAdsCampaignReportRow = {
  id: string;
  customer_id: string;
  report_date: string;
  campaign_id: string;
  campaign_name: string;
  campaign_status: string;
  impressions: number;
  clicks: number;
  cost_micros: number;
  cost: number | string;
  conversions: number;
  conversions_value: number;
};
type GoogleAdsReportSummary = {
  currency_code?: string | null;
  total_impressions: number;
  total_clicks: number;
  total_cost_micros?: number;
  total_cost: number | string;
  total_conversions: number;
  total_conversions_value?: number;
};
type GA4Property = {
  property_id: string;
  display_name: string;
  account_id: string;
  account_name: string;
};
type GA4ReportRow = {
  id: string;
  property_id: string;
  report_date: string;
  channel_group: string;
  sessions: number;
  active_users: number;
  event_count: number;
};
type Custom = {
  id: string;
  name: string;
  base_url: string;
  endpoint_path: string;
  auth_type: string;
  has_secret: boolean;
};

type OrganizationSummary = {
  id: string;
  name: string;
  role: "owner" | "admin" | "viewer";
};

type DashboardProps = {
  organizationId?: string;
  organizations?: OrganizationSummary[];
  onOrganizationChange?: (id: string) => void;
  previewMode?: boolean;
  previewWorkspace?: { name: string };
};

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const iconMap: Record<string, string> = {
  google_analytics: "◔",
  google_ads: "G",
  meta_ads: "∞",
  supermetrics: "S",
  groq: "✳",
};

export default function Dashboard({
  organizationId,
  organizations = [],
  onOrganizationChange,
  previewMode = false,
  previewWorkspace,
}: DashboardProps = {}) {
  const { user, signOut } = useAuth();
  const [tab, setTab] = useState("Overview");
  const [providers, setProviders] = useState<Provider[]>(
    previewMode ? PREVIEW_PROVIDERS : [],
  );
  const [connections, setConnections] = useState<Connection[]>([]);
  const [customs, setCustoms] = useState<Custom[]>([]);
  const [loading, setLoading] = useState(!previewMode);
  const [modal, setModal] = useState(false);
  const [toast, setToast] = useState("");
  const [query, setQuery] = useState("");
  const [form, setForm] = useState({
    name: "",
    base_url: "https://api.example.com",
    endpoint_path: "/",
    auth_type: "bearer",
    secret: "",
    secret_label: "X-API-Key",
    headers: "{}",
  });
  const [testResult, setTestResult] = useState<any>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);

  const [propModalConn, setPropModalConn] = useState<Connection | null>(null);
  const [gaProperties, setGaProperties] = useState<GA4Property[]>([]);
  const [loadingProps, setLoadingProps] = useState(false);
  const [selectedPropId, setSelectedPropId] = useState<string>("");
  const [savingProp, setSavingProp] = useState(false);

  const [reportModalConn, setReportModalConn] = useState<Connection | null>(null);
  const [gaReports, setGaReports] = useState<GA4ReportRow[]>([]);
  const [loadingReports, setLoadingReports] = useState(false);
  const [syncingConnId, setSyncingConnId] = useState<string | null>(null);

  const [adsCustomerModalConn, setAdsCustomerModalConn] = useState<Connection | null>(null);
  const [adsCustomers, setAdsCustomers] = useState<GoogleAdsCustomer[]>([]);
  const [loadingAdsCustomers, setLoadingAdsCustomers] = useState(false);
  const [selectedAdsCustomerId, setSelectedAdsCustomerId] = useState<string>("");
  const [savingAdsCustomer, setSavingAdsCustomer] = useState(false);

  const [adsReportModalConn, setAdsReportModalConn] = useState<Connection | null>(null);
  const [adsReports, setAdsReports] = useState<GoogleAdsCampaignReportRow[]>([]);
  const [adsReportSummary, setAdsReportSummary] = useState<GoogleAdsReportSummary | null>(null);
  const [loadingAdsReports, setLoadingAdsReports] = useState(false);

  const [metaAccountModalConn, setMetaAccountModalConn] = useState<Connection | null>(null);
  const [metaAccounts, setMetaAccounts] = useState<MetaAdsAccount[]>([]);
  const [loadingMetaAccounts, setLoadingMetaAccounts] = useState(false);
  const [selectedMetaAccountId, setSelectedMetaAccountId] = useState<string>("");
  const [savingMetaAccount, setSavingMetaAccount] = useState(false);

  const [metaReportModalConn, setMetaReportModalConn] = useState<Connection | null>(null);
  const [metaReports, setMetaReports] = useState<MetaAdsCampaignReportRow[]>([]);
  const [metaReportSummary, setMetaReportSummary] = useState<MetaAdsReportSummary | null>(null);
  const [loadingMetaReports, setLoadingMetaReports] = useState(false);

  // Supermetrics state
  const [smConfigModalConn, setSmConfigModalConn] = useState<Connection | null>(null);
  const [smApiKey, setSmApiKey] = useState("");
  const [savingSmConfig, setSavingSmConfig] = useState(false);

  const [smSourceModalConn, setSmSourceModalConn] = useState<Connection | null>(null);
  const [smSources, setSmSources] = useState<SupermetricsSource[]>([]);
  const [loadingSmSources, setLoadingSmSources] = useState(false);
  const [selectedSmDsId, setSelectedSmDsId] = useState("");
  const [smAccountsInput, setSmAccountsInput] = useState("");
  const [savingSmSource, setSavingSmSource] = useState(false);

  const [smReportModalConn, setSmReportModalConn] = useState<Connection | null>(null);
  const [smReports, setSmReports] = useState<SupermetricsReportRow[]>([]);
  const [smReportSummary, setSmReportSummary] = useState<SupermetricsReportSummary | null>(null);
  const [loadingSmReports, setLoadingSmReports] = useState(false);

  // Groq state
  const [groqConfigModalConn, setGroqConfigModalConn] = useState<Connection | null>(null);
  const [groqApiKey, setGroqApiKey] = useState("");
  const [groqSelectedModel, setGroqSelectedModel] = useState("llama-3.3-70b-versatile");
  const [groqTemperature, setGroqTemperature] = useState(0.7);
  const [groqMaxTokens, setGroqMaxTokens] = useState(256);
  const [groqModels, setGroqModels] = useState<GroqModelItem[]>([]);
  const [loadingGroqModels, setLoadingGroqModels] = useState(false);
  const [savingGroqConfig, setSavingGroqConfig] = useState(false);

  const [groqTestModalConn, setGroqTestModalConn] = useState<Connection | null>(null);
  const [groqPrompt, setGroqPrompt] = useState("Explain data connectors in one sentence.");
  const [runningGroqTest, setRunningGroqTest] = useState(false);
  const [groqResult, setGroqResult] = useState<GroqCompletionResult | null>(null);

  const currentOrg = organizations.find((o) => o.id === organizationId);
  const isViewer = previewMode || currentOrg?.role === "viewer";
  const orgName = previewWorkspace?.name || currentOrg?.name || "Workspace";
  const orgRole = currentOrg?.role
    ? currentOrg.role.charAt(0).toUpperCase() + currentOrg.role.slice(1)
    : "Member";
  const unavailableActionTitle = previewMode
    ? "Unavailable in preview mode"
    : "Viewer role has read-only access";
  const displayName = user?.displayName || user?.email?.split("@")[0] || "User";
  const userInitials = (user?.displayName || user?.email || "U")
    .slice(0, 2)
    .toUpperCase();

  async function apiFetch(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    if (previewMode) {
      setToast("Unavailable in preview mode.");
      throw new Error("Unavailable in preview mode.");
    }
    return init
      ? authenticatedApiFetch(input, init)
      : authenticatedApiFetch(input);
  }

  async function load() {
    setLoading(true);
    if (previewMode) {
      setProviders(PREVIEW_PROVIDERS);
      setConnections([]);
      setCustoms([]);
      setLoading(false);
      return;
    }
    try {
      const orgQuery = organizationId
        ? `?organization_id=${encodeURIComponent(organizationId)}`
        : "";
      const [p, c, x] = await Promise.all([
        apiFetch(`${API}/providers`),
        organizationId
          ? apiFetch(`${API}/connections${orgQuery}`)
          : Promise.resolve(new Response("[]")),
        organizationId
          ? apiFetch(`${API}/custom-connectors${orgQuery}`)
          : Promise.resolve(new Response("[]")),
      ]);
      if (!p.ok || !c.ok || !x.ok) throw new Error("API unavailable");
      setProviders(await p.json());
      setConnections(await c.json());
      setCustoms(await x.json());
    } catch {
      setToast("Backend not connected. Start the API with the quick-start guide.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    if (typeof window !== "undefined") {
      const urlParams = new URLSearchParams(window.location.search);
      const statusParam = urlParams.get("status");
      if (statusParam === "needs_configuration") {
        setToast("Google account authorized! Please select a GA4 property.");
      }
      if (urlParams.get("google_ads_connected") === "true") {
        setToast("Google Ads account authorized! Please select a customer account.");
      }
      if (urlParams.get("meta_ads_connected") === "true") {
        setToast("Meta account authorized! Please select an ad account.");
      }
    }
  }, [organizationId]);

  const connected = connections.filter((c) => c.status === "connected").length;
  const filtered = providers.filter((p) =>
    (p.name + p.description + p.category)
      .toLowerCase()
      .includes(query.toLowerCase()),
  );

  async function startGoogleOAuth(connId?: string) {
    if (!organizationId) {
      setToast("Please select a workspace before connecting.");
      return;
    }
    if (isViewer) {
      setToast("Viewers have read-only access and cannot authorize integrations.");
      return;
    }
    try {
      const url = `${API}/oauth/google/authorize${connId ? `?connection_id=${encodeURIComponent(connId)}` : ""}`;
      const r = await apiFetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not initiate Google OAuth.");
      }
      const data = await r.json();
      if (data.authorization_url) {
        window.location.href = data.authorization_url;
      }
    } catch (e: any) {
      setToast(e.message || "Failed to start Google OAuth flow.");
    }
  }

  async function startGoogleAdsOAuth(connId?: string) {
    if (!organizationId) {
      setToast("Please select a workspace before connecting.");
      return;
    }
    if (isViewer) {
      setToast("Viewers have read-only access and cannot authorize integrations.");
      return;
    }
    try {
      const url = `${API}/oauth/google-ads/authorize${connId ? `?connection_id=${encodeURIComponent(connId)}` : ""}`;
      const r = await apiFetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to initiate Google Ads OAuth.");
      }
      const d = await r.json();
      if (d.authorization_url) {
        window.location.href = d.authorization_url;
      }
    } catch (e: any) {
      setToast(e.message || "Failed to start Google Ads authorization.");
    }
  }

  async function openAdsCustomerModal(c: Connection) {
    if (!organizationId) return;
    setAdsCustomerModalConn(c);
    setLoadingAdsCustomers(true);
    setAdsCustomers([]);
    setSelectedAdsCustomerId(c.customer_id || "");
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/google-ads/customers`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not retrieve Google Ads customers.");
      }
      const data = await r.json();
      const custs: GoogleAdsCustomer[] = data.customers || [];
      setAdsCustomers(custs);
      if (custs.length > 0 && !c.customer_id) {
        setSelectedAdsCustomerId(custs[0].customer_id);
      }
    } catch (e: any) {
      setToast(e.message || "Failed to load Google Ads customer accounts.");
    } finally {
      setLoadingAdsCustomers(false);
    }
  }

  async function saveSelectedAdsCustomer() {
    if (!adsCustomerModalConn || !organizationId || !selectedAdsCustomerId) return;
    setSavingAdsCustomer(true);
    const chosen = adsCustomers.find((c) => c.customer_id === selectedAdsCustomerId);
    try {
      const r = await apiFetch(`${API}/connections/${adsCustomerModalConn.id}/google-ads/customers/select`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          customer_id: selectedAdsCustomerId,
          customer_name: chosen?.descriptive_name || `Account ${selectedAdsCustomerId}`,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to select Google Ads customer.");
      }
      setToast("Google Ads customer account connected successfully!");
      setAdsCustomerModalConn(null);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to save Google Ads customer.");
    } finally {
      setSavingAdsCustomer(false);
    }
  }

  async function triggerGoogleAdsSync(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers have read-only access and cannot trigger sync.");
      return;
    }
    setSyncingConnId(c.id);
    try {
      const r = await apiFetch(`${API}/sync-jobs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          connection_id: c.id,
          job_type: "google_ads_sync",
          metadata: {
            start_date: "30daysAgo",
            end_date: "yesterday",
          },
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to trigger Google Ads sync.");
      }
      const job = await r.json();
      setToast(`Google Ads sync queued (${job.status || "queued"}).`);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to trigger Google Ads sync.");
    } finally {
      setSyncingConnId(null);
    }
  }

  async function openAdsReportsModal(c: Connection) {
    if (!organizationId) return;
    setAdsReportModalConn(c);
    setLoadingAdsReports(true);
    setAdsReports([]);
    setAdsReportSummary(null);
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/google-ads/reports?limit=100`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to load Google Ads reports.");
      }
      const d = await r.json();
      setAdsReports(d.items || []);
      setAdsReportSummary(d.summary || null);
    } catch (e: any) {
      setToast(e.message || "Failed to load Google Ads report data.");
    } finally {
      setLoadingAdsReports(false);
    }
  }

  async function disconnectGoogleAds(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers cannot disconnect integrations.");
      return;
    }
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/google-ads/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to disconnect Google Ads.");
      }
      setToast("Google Ads disconnected successfully.");
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to disconnect Google Ads.");
    }
  }

  async function startMetaAdsOAuth(connId?: string) {
    if (!organizationId) {
      setToast("Please select a workspace before connecting.");
      return;
    }
    if (isViewer) {
      setToast("Viewers have read-only access and cannot authorize integrations.");
      return;
    }
    try {
      const url = `${API}/oauth/meta-ads/authorize${connId ? `?connection_id=${encodeURIComponent(connId)}` : ""}`;
      const r = await apiFetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to initiate Meta Ads OAuth.");
      }
      const d = await r.json();
      if (d.authorization_url) {
        window.location.href = d.authorization_url;
      }
    } catch (e: any) {
      setToast(e.message || "Failed to start Meta Ads authorization.");
    }
  }

  async function openMetaAccountModal(c: Connection) {
    if (!organizationId) return;
    setMetaAccountModalConn(c);
    setLoadingMetaAccounts(true);
    setMetaAccounts([]);
    setSelectedMetaAccountId(c.ad_account_id || "");
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/meta-ads/accounts`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not retrieve Meta ad accounts.");
      }
      const data = await r.json();
      const accts: MetaAdsAccount[] = data.accounts || [];
      setMetaAccounts(accts);
      if (accts.length > 0 && !c.ad_account_id) {
        setSelectedMetaAccountId(accts[0].account_id);
      }
    } catch (e: any) {
      setToast(e.message || "Failed to load Meta ad accounts.");
    } finally {
      setLoadingMetaAccounts(false);
    }
  }

  async function saveSelectedMetaAccount() {
    if (!metaAccountModalConn || !organizationId || !selectedMetaAccountId) return;
    setSavingMetaAccount(true);
    const chosen = metaAccounts.find((a) => a.account_id === selectedMetaAccountId);
    try {
      const r = await apiFetch(`${API}/connections/${metaAccountModalConn.id}/meta-ads/accounts/select`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          account_id: selectedMetaAccountId,
          name: chosen?.name || `Account ${selectedMetaAccountId}`,
          currency: chosen?.currency || undefined,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to select Meta ad account.");
      }
      setToast("Meta ad account connected successfully!");
      setMetaAccountModalConn(null);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to save Meta ad account.");
    } finally {
      setSavingMetaAccount(false);
    }
  }

  async function triggerMetaAdsSync(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers have read-only access and cannot trigger sync.");
      return;
    }
    setSyncingConnId(c.id);
    try {
      const r = await apiFetch(`${API}/sync-jobs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          connection_id: c.id,
          job_type: "meta_ads_sync",
          metadata: {
            start_date: "30daysAgo",
            end_date: "yesterday",
          },
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to trigger Meta Ads sync.");
      }
      const job = await r.json();
      setToast(`Meta Ads sync queued (${job.status || "queued"}).`);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to trigger Meta Ads sync.");
    } finally {
      setSyncingConnId(null);
    }
  }

  async function openMetaReportsModal(c: Connection) {
    if (!organizationId) return;
    setMetaReportModalConn(c);
    setLoadingMetaReports(true);
    setMetaReports([]);
    setMetaReportSummary(null);
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/meta-ads/reports?limit=100`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to load Meta Ads reports.");
      }
      const d = await r.json();
      setMetaReports(d.items || []);
      setMetaReportSummary(d.summary || null);
    } catch (e: any) {
      setToast(e.message || "Failed to load Meta Ads report data.");
    } finally {
      setLoadingMetaReports(false);
    }
  }

  async function disconnectMetaAds(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers cannot disconnect integrations.");
      return;
    }
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/meta-ads/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to disconnect Meta Ads.");
      }
      setToast("Meta Ads disconnected successfully.");
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to disconnect Meta Ads.");
    }
  }

  async function openPropertyModal(c: Connection) {
    if (!organizationId) return;
    setPropModalConn(c);
    setLoadingProps(true);
    setGaProperties([]);
    setSelectedPropId(c.property_id || "");
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/google-analytics/properties`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not retrieve GA4 properties.");
      }
      const props: GA4Property[] = await r.json();
      setGaProperties(props);
      if (props.length > 0 && !c.property_id) {
        setSelectedPropId(props[0].property_id);
      }
    } catch (e: any) {
      setToast(e.message || "Failed to load GA4 properties.");
    } finally {
      setLoadingProps(false);
    }
  }

  async function saveSelectedProperty() {
    if (!propModalConn || !organizationId || !selectedPropId) return;
    setSavingProp(true);
    const chosen = gaProperties.find((p) => p.property_id === selectedPropId);
    try {
      const r = await apiFetch(`${API}/connections/${propModalConn.id}/google-analytics/properties/select`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          property_id: selectedPropId,
          display_name: chosen?.display_name,
          account_id: chosen?.account_id,
          account_name: chosen?.account_name,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to select property.");
      }
      setToast("GA4 property connected successfully!");
      setPropModalConn(null);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to save GA4 property.");
    } finally {
      setSavingProp(false);
    }
  }

  async function triggerSync(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers have read-only access and cannot trigger sync jobs.");
      return;
    }
    setSyncingConnId(c.id);
    try {
      const r = await apiFetch(`${API}/sync-jobs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          connection_id: c.id,
          job_type: "ga4_sync",
          metadata: {
            start_date: "30daysAgo",
            end_date: "yesterday",
          },
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not create sync job.");
      }
      const data = await r.json();
      setToast(`Sync job created (${data.status}).`);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to trigger sync.");
    } finally {
      setSyncingConnId(null);
    }
  }

  async function openReportsModal(c: Connection) {
    if (!organizationId) return;
    setReportModalConn(c);
    setLoadingReports(true);
    setGaReports([]);
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/google-analytics/reports?limit=100`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to load reports.");
      }
      const data = await r.json();
      setGaReports(data.items || []);
    } catch (e: any) {
      setToast(e.message || "Failed to load GA4 report data.");
    } finally {
      setLoadingReports(false);
    }
  }

  async function disconnectConnection(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers have read-only access and cannot disconnect integrations.");
      return;
    }
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not disconnect integration.");
      }
      setToast("Integration disconnected successfully.");
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to disconnect integration.");
    }
  }

  // --- Supermetrics Handlers ---
  function openSmConfigModal(c: Connection) {
    setSmConfigModalConn(c);
    setSmApiKey("");
  }

  async function saveSmConfig() {
    if (!smConfigModalConn || !organizationId || !smApiKey.trim()) return;
    setSavingSmConfig(true);
    try {
      const r = await apiFetch(`${API}/connections/${smConfigModalConn.id}/supermetrics/config`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({ api_key: smApiKey.trim() }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to validate and save Supermetrics API key.");
      }
      setToast("Supermetrics API key verified and stored securely!");
      const updatedConn = { ...smConfigModalConn, has_secret: true, status: "needs_source_selection" };
      setSmConfigModalConn(null);
      load();
      openSmSourceModal(updatedConn);
    } catch (e: any) {
      setToast(e.message || "Failed to save Supermetrics API key.");
    } finally {
      setSavingSmConfig(false);
    }
  }

  async function openSmSourceModal(c: Connection) {
    if (!organizationId) return;
    setSmSourceModalConn(c);
    setLoadingSmSources(true);
    setSmSources([]);
    setSelectedSmDsId(c.ds_id || "GAWA");
    setSmAccountsInput(c.ds_accounts || "");
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/supermetrics/sources`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to load Supermetrics sources.");
      }
      const data = await r.json();
      const sources: SupermetricsSource[] = data.sources || [];
      setSmSources(sources);
      if (sources.length > 0 && !c.ds_id) {
        setSelectedSmDsId(sources[0].ds_id);
      }
    } catch (e: any) {
      setToast(e.message || "Failed to load Supermetrics data sources.");
    } finally {
      setLoadingSmSources(false);
    }
  }

  async function saveSmSource() {
    if (!smSourceModalConn || !organizationId || !selectedSmDsId) return;
    setSavingSmSource(true);
    const chosen = smSources.find((s) => s.ds_id === selectedSmDsId);
    try {
      const r = await apiFetch(`${API}/connections/${smSourceModalConn.id}/supermetrics/sources/select`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          ds_id: selectedSmDsId,
          ds_name: chosen?.name || selectedSmDsId,
          ds_accounts: smAccountsInput.trim() || undefined,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to select data source.");
      }
      setToast("Supermetrics data source selected successfully!");
      setSmSourceModalConn(null);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to save Supermetrics source.");
    } finally {
      setSavingSmSource(false);
    }
  }

  async function triggerSupermetricsSync(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers have read-only access and cannot trigger sync.");
      return;
    }
    setSyncingConnId(c.id);
    try {
      const r = await apiFetch(`${API}/sync-jobs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          connection_id: c.id,
          job_type: "supermetrics_sync",
          metadata: {
            start_date: "30daysAgo",
            end_date: "yesterday",
          },
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to trigger Supermetrics sync.");
      }
      const job = await r.json();
      setToast(`Supermetrics sync queued (${job.status || "queued"}).`);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to trigger Supermetrics sync.");
    } finally {
      setSyncingConnId(null);
    }
  }

  async function openSmReportsModal(c: Connection) {
    if (!organizationId) return;
    setSmReportModalConn(c);
    setLoadingSmReports(true);
    setSmReports([]);
    setSmReportSummary(null);
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/supermetrics/reports?limit=100`, {
        headers: { "X-Organization-ID": organizationId },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to load Supermetrics reports.");
      }
      const d = await r.json();
      setSmReports(d.items || []);
      setSmReportSummary(d.summary || null);
    } catch (e: any) {
      setToast(e.message || "Failed to load Supermetrics report data.");
    } finally {
      setLoadingSmReports(false);
    }
  }

  async function disconnectSupermetrics(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers cannot disconnect integrations.");
      return;
    }
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/supermetrics/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to disconnect Supermetrics.");
      }
      setToast("Supermetrics disconnected successfully.");
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to disconnect Supermetrics.");
    }
  }

  // --- Groq Handlers ---
  async function openGroqConfigModal(c: Connection) {
    setGroqConfigModalConn(c);
    setGroqApiKey("");
    setGroqSelectedModel(c.selected_model || "llama-3.3-70b-versatile");
    if (c.has_secret && organizationId) {
      setLoadingGroqModels(true);
      try {
        const r = await apiFetch(`${API}/connections/${c.id}/groq/models`, {
          headers: { "X-Organization-ID": organizationId },
        });
        if (r.ok) {
          const d = await r.json();
          setGroqModels(d.models || []);
        }
      } catch {
        // fallback
      } finally {
        setLoadingGroqModels(false);
      }
    }
  }

  async function saveGroqConfig() {
    if (!groqConfigModalConn || !organizationId || !groqApiKey.trim()) return;
    setSavingGroqConfig(true);
    try {
      const r = await apiFetch(`${API}/connections/${groqConfigModalConn.id}/groq/config`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          api_key: groqApiKey.trim(),
          selected_model: groqSelectedModel,
          temperature: groqTemperature,
          max_tokens: groqMaxTokens,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to configure Groq.");
      }
      setToast("Groq API key validated and configured successfully!");
      setGroqConfigModalConn(null);
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to save Groq configuration.");
    } finally {
      setSavingGroqConfig(false);
    }
  }

  function openGroqTestModal(c: Connection) {
    setGroqTestModalConn(c);
    setGroqPrompt("Explain why real-time data sync matters in one concise sentence.");
    setGroqResult(null);
  }

  async function runGroqCompletionTest() {
    if (!groqTestModalConn || !organizationId || !groqPrompt.trim()) return;
    setRunningGroqTest(true);
    setGroqResult(null);
    try {
      const r = await apiFetch(`${API}/connections/${groqTestModalConn.id}/groq/test-completion`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          prompt: groqPrompt.trim(),
          model: groqTestModalConn.selected_model || undefined,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Groq completion test failed.");
      }
      const d = await r.json();
      setGroqResult(d);
      setToast("Groq test completion succeeded!");
    } catch (e: any) {
      setToast(e.message || "Failed to run Groq completion.");
    } finally {
      setRunningGroqTest(false);
    }
  }

  async function disconnectGroq(c: Connection) {
    if (!organizationId) return;
    if (isViewer) {
      setToast("Viewers cannot disconnect integrations.");
      return;
    }
    try {
      const r = await apiFetch(`${API}/connections/${c.id}/groq/disconnect`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to disconnect Groq.");
      }
      setToast("Groq disconnected successfully.");
      load();
    } catch (e: any) {
      setToast(e.message || "Failed to disconnect Groq.");
    }
  }

  async function connect(p: Provider) {
    if (!organizationId) {
      setToast("Please select a workspace before connecting a platform.");
      return;
    }
    if (isViewer) {
      setToast("Viewers have read-only access and cannot connect platforms.");
      return;
    }
    if (p.key === "google_analytics") {
      await startGoogleOAuth();
      return;
    }
    if (p.key === "google_ads") {
      await startGoogleAdsOAuth();
      return;
    }
    if (p.key === "meta_ads") {
      await startMetaAdsOAuth();
      return;
    }
    if (p.key === "supermetrics") {
      const existing = connections.find((c) => c.provider_key === "supermetrics");
      if (existing) {
        if (!existing.has_secret) {
          openSmConfigModal(existing);
        } else {
          openSmSourceModal(existing);
        }
        return;
      }
      try {
        const r = await apiFetch(`${API}/connections`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Organization-ID": organizationId,
          },
          body: JSON.stringify({
            organization_id: organizationId,
            provider_key: "supermetrics",
            display_name: p.name,
          }),
        });
        if (!r.ok) throw new Error("Could not create Supermetrics connection.");
        const newConn = await r.json();
        load();
        openSmConfigModal(newConn);
        return;
      } catch (e: any) {
        setToast(e.message || "Failed to initialize Supermetrics connection.");
        return;
      }
    }
    if (p.key === "groq") {
      const existing = connections.find((c) => c.provider_key === "groq");
      if (existing) {
        openGroqConfigModal(existing);
        return;
      }
      try {
        const r = await apiFetch(`${API}/connections`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Organization-ID": organizationId,
          },
          body: JSON.stringify({
            organization_id: organizationId,
            provider_key: "groq",
            display_name: p.name,
          }),
        });
        if (!r.ok) throw new Error("Could not create Groq connection.");
        const newConn = await r.json();
        load();
        openGroqConfigModal(newConn);
        return;
      } catch (e: any) {
        setToast(e.message || "Failed to initialize Groq connection.");
        return;
      }
    }
    try {
      const r = await apiFetch(`${API}/connections`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          organization_id: organizationId,
          provider_key: p.key,
          display_name: p.name,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Could not create connection.");
      }
      const d = await r.json();
      setToast(d.message);
      load();
    } catch (e: any) {
      setToast(e.message || "Could not create connection. Check that the backend is running.");
    }
  }

  async function testCustom() {
    if (!organizationId) {
      setToast("Please select a workspace before testing a connector.");
      return;
    }
    if (isViewer) {
      setToast("Viewers have read-only access and cannot test connectors.");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      let parsedHeaders = {};
      try {
        parsedHeaders = JSON.parse(form.headers || "{}");
      } catch {
        throw new Error("Invalid JSON in additional headers.");
      }
      const r = await apiFetch(`${API}/custom-connectors/test`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          base_url: form.base_url,
          endpoint_path: form.endpoint_path,
          auth_type: form.auth_type,
          secret: form.secret || null,
          secret_label: form.secret_label || null,
          headers: parsedHeaders,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Test failed");
      setTestResult(d);
    } catch (e: any) {
      setTestResult({
        success: false,
        message: e.message || "Could not test endpoint.",
      });
    } finally {
      setTesting(false);
    }
  }

  async function saveCustom() {
    if (!organizationId) {
      setToast("Please select a workspace before saving a connector.");
      return;
    }
    if (isViewer) {
      setToast("Viewers have read-only access and cannot create custom connectors.");
      return;
    }
    setSaving(true);
    try {
      let parsedHeaders = {};
      try {
        parsedHeaders = JSON.parse(form.headers || "{}");
      } catch {
        throw new Error("Invalid JSON in additional headers.");
      }
      const r = await apiFetch(`${API}/custom-connectors`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Organization-ID": organizationId,
        },
        body: JSON.stringify({
          organization_id: organizationId,
          name: form.name,
          base_url: form.base_url,
          endpoint_path: form.endpoint_path,
          auth_type: form.auth_type,
          secret: form.secret || null,
          secret_label: form.secret_label || null,
          headers: parsedHeaders,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not save connector");
      setToast("Custom connector saved.");
      setModal(false);
      setForm({
        name: "",
        base_url: "https://api.example.com",
        endpoint_path: "/",
        auth_type: "bearer",
        secret: "",
        secret_label: "X-API-Key",
        headers: "{}",
      });
      load();
    } catch (e: any) {
      setToast(e.message || "Could not save connector.");
    } finally {
      setSaving(false);
    }
  }

  async function removeCustom(id: string) {
    if (isViewer) {
      setToast("Viewers have read-only access and cannot delete custom connectors.");
      return;
    }
    if (!confirm("Delete this custom connector?")) return;
    try {
      const r = await apiFetch(
        `${API}/custom-connectors/${encodeURIComponent(id)}`,
        {
          method: "DELETE",
          headers: { "X-Organization-ID": organizationId! },
        },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to delete custom connector.");
      }
      load();
    } catch (e: any) {
      setToast(e.message || "Could not delete connector.");
    }
  }

  async function removeConnection(id: string) {
    if (isViewer) {
      setToast("Viewers have read-only access and cannot delete connections.");
      return;
    }
    if (!confirm("Delete this connection?")) return;
    try {
      const r = await apiFetch(
        `${API}/connections/${encodeURIComponent(id)}`,
        {
          method: "DELETE",
          headers: { "X-Organization-ID": organizationId! },
        },
      );
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to delete connection.");
      }
      load();
    } catch (e: any) {
      setToast(e.message || "Could not delete connection.");
    }
  }

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brandmark">
            <Zap size={19} fill="currentColor" />
          </div>
          <span>
            orbit<span className="branddot">.</span>
          </span>
          <span className="workspace">WORKSPACE</span>
        </div>
        <div className="workspace-switch">
          <div className="avatar">{orgName.charAt(0).toUpperCase()}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <b>{orgName}</b>
            <small>{previewMode ? "Local preview data only" : `${orgRole} role`}</small>
          </div>
          {organizations.length > 1 && onOrganizationChange && (
            <select
              aria-label="Switch organization"
              value={organizationId}
              onChange={(e) => onOrganizationChange(e.target.value)}
              style={{
                border: "none",
                background: "transparent",
                color: "inherit",
                cursor: "pointer",
                maxWidth: "20px",
              }}
            >
              {organizations.map((org) => (
                <option key={org.id} value={org.id} style={{ color: "#000" }}>
                  {org.name} ({org.role})
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="navlabel">WORKSPACE</div>
        {[
          { n: "Overview", i: LayoutDashboard },
          { n: "Integrations", i: Link2 },
          { n: "Data sources", i: Database },
          { n: "Sync activity", i: Activity },
        ].map((x) => (
          <button
            key={x.n}
            onClick={() => setTab(x.n)}
            className={`navitem ${tab === x.n ? "active" : ""}`}
          >
            <x.i size={17} />
            {x.n}
            {x.n === "Integrations" && (
              <span className="navcount">
                {connections.length + customs.length}
              </span>
            )}
          </button>
        ))}
        <div className="navlabel lower">PREFERENCES</div>
        <button
          className="navitem"
          onClick={() =>
            setToast(`Connected as ${user?.email || "anonymous"}. Organization: ${orgName} (${orgRole}).`)
          }
        >
          <Settings2 size={17} />
          Settings
        </button>
        <div className="sidebar-bottom">
          <div className="help">
            <CircleHelp size={17} />
            <span>
              Need a hand?<small>Read the setup guide</small>
            </span>
            <ArrowUpRight size={14} />
          </div>
          <div className="userrow">
            <div className="avatar small">{userInitials}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <b>{displayName}</b>
              <small>{user?.email || "Authenticated"}</small>
            </div>
            <button
              className="iconbtn"
              onClick={() => void signOut()}
              title="Sign out"
              aria-label="Sign out"
              style={{ width: 28, height: 28 }}
            >
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </aside>
      <section className="main">
        <header className="topbar">
          <div className="crumb">
            Workspace <span>/</span> <b>{tab}</b>
          </div>
          <div className="topright">
            <div className={`status${previewMode ? " preview-status" : ""}`}>
              <i /> {previewMode ? "Preview mode · backend actions unavailable" : "All systems operational"}
            </div>
            <button className="iconbtn" aria-label="Notifications">
              <Bell size={18} />
            </button>
            <div className="avatar small" title={user?.email || ""}>
              {userInitials}
            </div>
          </div>
        </header>
        <div className="content">
          {previewMode && (
            <div className="preview-banner" role="status">
              <strong>PREVIEW MODE</strong>
              <span>
                Demo content is illustrative, not live customer data. Backend
                actions are unavailable in preview mode.
              </span>
            </div>
          )}
          <div className="welcome">
            <div>
              <div className="eyebrow">
                <span className="eyebrowline" /> YOUR DATA, ONE PLACE
              </div>
              <h1>
                {tab === "Overview" ? `Good morning, ${displayName}.` : tab}
              </h1>
              <p>
                {tab === "Overview"
                  ? "Bring your marketing and analytics tools together. No more jumping between tabs."
                  : "Manage your connected platforms and custom data sources."}
              </p>
            </div>
            <button
              className="primary"
              disabled={isViewer}
              title={isViewer ? unavailableActionTitle : "Add a connector"}
              onClick={() => setModal(true)}
            >
              <Plus size={17} /> Add a connector
            </button>
          </div>
          {previewMode && tab === "Overview" && (
            <section className="preview-metrics" aria-label="Illustrative demo data">
              <div className="preview-metrics-heading">
                <h2>Illustrative demo data</h2>
                <p>Sample values only · not connected to a live account</p>
              </div>
              <div className="preview-metrics-grid">
                {PREVIEW_METRICS.map((metric) => (
                  <article className="statcard" key={metric.label}>
                    <div className="stathead">{metric.label.toUpperCase()}</div>
                    <div className="statvalue">{metric.value}</div>
                    <div className="statfoot">{metric.period}</div>
                  </article>
                ))}
              </div>
            </section>
          )}
          <div className="stats">
            <div className="statcard">
              <div className="stathead">
                CONNECTED PLATFORMS <Link2 size={16} />
              </div>
              <div className="statvalue">
                {connected + customs.length}
                <span className="statmuted">
                  {" "}
                  / {providers.length + customs.length}
                </span>
              </div>
              <div className="statfoot">
                <span className="green-dot" /> {customs.length} custom ·{" "}
                {connections.length} provider records
              </div>
            </div>
            <div className="statcard">
              <div className="stathead">
                DATA SOURCES <Database size={16} />
              </div>
              <div className="statvalue">
                {customs.length}
                <span className="statmuted"> custom</span>
              </div>
              <div className="statfoot">
                {previewMode ? "Unavailable in preview mode" : "Ready to configure for ingestion"}
              </div>
            </div>
            <div className="statcard">
              <div className="stathead">
                SYNC HEALTH <Activity size={16} />
              </div>
              <div className="statvalue">—</div>
              <div className="statfoot">
                {previewMode ? "No live sync activity in preview mode" : "Sync engine setup is the next step"}
              </div>
            </div>
            <div className="statcard accentstat">
              <div className="stathead">
                WORKSPACE SECURITY <ShieldCheck size={16} />
              </div>
              <div className="statvalue">{previewMode ? "Demo" : "Protected"}</div>
              <div className="statfoot">
                {previewMode
                  ? "Firebase authentication is active; no live workspace data"
                  : "Firebase authenticated · Credentials encrypted at rest"}
              </div>
            </div>
          </div>
          <div className="sectiontop">
            <div>
              <h2>Connect your platforms</h2>
              <p>Choose a ready-made integration or bring your own API.</p>
            </div>
            <button className="textbtn" onClick={() => setTab("Integrations")}>
              View all integrations <ArrowUpRight size={15} />
            </button>
          </div>
          <div className="searchrow">
            <div className="searchbox">
              <Search size={16} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search platforms..."
              />
            </div>
            <span className="tinylabel">{providers.length} AVAILABLE</span>
          </div>
          <div className="providergrid">
            {filtered.map((p) => (
              <article className="provider" key={p.key}>
                <div className={`providerlogo ${p.key}`}>
                  {iconMap[p.key] || "✳"}
                </div>
                <div className="provider-meta">
                  <span className="category">{p.category}</span>
                  <span className="providerstate">
                    {p.setup_state === "scaffolded" ? "SETUP REQUIRED" : "READY"}
                  </span>
                </div>
                <h3>{p.name}</h3>
                <p>{p.description}</p>
                <div className="providerfoot">
                  <span className="auth">{p.auth_type}</span>
                  <button
                    className="connectbtn"
                    disabled={isViewer}
                    title={isViewer ? unavailableActionTitle : "Connect"}
                    onClick={() => connect(p)}
                  >
                    Connect <ArrowUpRight size={14} />
                  </button>
                </div>
              </article>
            ))}
            <article className="provider custom-provider">
              <div className="providerlogo customlogo">
                <Plus size={21} />
              </div>
              <div className="provider-meta">
                <span className="category">CUSTOM SOURCE</span>
                <span className="providerstate available">FLEXIBLE</span>
              </div>
              <h3>Bring your own API</h3>
              <p>
                Connect any HTTPS REST API with a simple endpoint and your
                preferred authentication.
              </p>
              <div className="providerfoot">
                <span className="auth">REST · JSON</span>
                <button
                  className="connectbtn"
                  disabled={isViewer}
                  title={isViewer ? unavailableActionTitle : "Set up"}
                  onClick={() => setModal(true)}
                >
                  Set up <ArrowUpRight size={14} />
                </button>
              </div>
            </article>
          </div>
          <div className="bottomgrid">
            <section className="panel">
              <div className="panelhead">
                <div>
                  <h2>Your connections</h2>
                  <p>Recently added data sources</p>
                </div>
                <button
                  className="iconbtn"
                  onClick={load}
                  disabled={previewMode}
                  title={previewMode ? "Unavailable in preview mode" : "Refresh connections"}
                  aria-label="Refresh connections"
                >
                  <RefreshCw size={16} />
                </button>
              </div>
              {connections.length === 0 && customs.length === 0 ? (
                <div className="empty">
                  <div className="emptyicon">
                    <Cloud size={22} />
                  </div>
                  <b>{previewMode ? "No live integrations in preview" : "Your workspace is ready"}</b>
                  <span>
                    {previewMode
                      ? "Connections are unavailable in preview mode."
                      : "Connect your first platform to get started."}
                  </span>
                </div>
              ) : (
                <div className="connectionlist">
                  {connections.map((c) => (
                    <div className="connectionrow" key={c.id}>
                      <div className="miniicon">
                        {iconMap[c.provider_key] || "✳"}
                      </div>
                      <div className="connectionname">
                        <b>{c.display_name}</b>
                        <small>
                          {c.provider_key === "google_ads"
                            ? (c.customer_name ? `${c.customer_name} (${c.customer_id})` : (c.customer_id || "Google Ads"))
                            : c.provider_key === "meta_ads"
                            ? (c.ad_account_name ? `${c.ad_account_name} (act_${c.ad_account_id})` : (c.ad_account_id ? `act_${c.ad_account_id}` : "Meta Ads"))
                            : c.provider_key === "supermetrics"
                            ? (c.ds_name ? `${c.ds_name} (${c.ds_id})${c.ds_accounts ? ` · ${c.ds_accounts}` : ""}` : (c.ds_id || "Supermetrics Enterprise v2"))
                            : c.provider_key === "groq"
                            ? (c.selected_model ? `Model: ${c.selected_model}` : "Groq LPU Inference")
                            : `${c.account_name ? `${c.account_name} · ` : ""}${c.property_name || c.property_id || c.provider_key.replaceAll("_", " ")}`}
                        </small>
                      </div>
                      {c.status === "connected" ? (
                        <>
                          <span className="saved">
                            <Check size={12} /> Connected
                          </span>
                          <div className="action-group">
                            {c.provider_key === "groq" ? (
                              <>
                                <button
                                  className="secondary smallbtn"
                                  onClick={() => openGroqTestModal(c)}
                                  title="Test Groq inference with a prompt"
                                >
                                  <Sparkles size={12} /> Test Prompt
                                </button>
                                <button
                                  className="secondary smallbtn"
                                  onClick={() => openGroqConfigModal(c)}
                                  title="Configure Groq models & settings"
                                >
                                  Config
                                </button>
                              </>
                            ) : (
                              <>
                                <button
                                  className="secondary smallbtn"
                                  disabled={isViewer || syncingConnId === c.id}
                                  onClick={() => {
                                    if (c.provider_key === "google_ads") triggerGoogleAdsSync(c);
                                    else if (c.provider_key === "meta_ads") triggerMetaAdsSync(c);
                                    else if (c.provider_key === "supermetrics") triggerSupermetricsSync(c);
                                    else triggerSync(c);
                                  }}
                                  title={
                                    previewMode
                                      ? unavailableActionTitle
                                      : isViewer
                                      ? "Viewer role cannot trigger sync"
                                      : "Trigger synchronization"
                                  }
                                >
                                  <RefreshCw size={12} className={syncingConnId === c.id ? "spin" : ""} /> Sync
                                </button>
                                <button
                                  className="secondary smallbtn"
                                  onClick={() => {
                                    if (c.provider_key === "google_ads") openAdsReportsModal(c);
                                    else if (c.provider_key === "meta_ads") openMetaReportsModal(c);
                                    else if (c.provider_key === "supermetrics") openSmReportsModal(c);
                                    else openReportsModal(c);
                                  }}
                                  title={
                                    c.provider_key === "google_ads"
                                      ? "View Google Ads campaign reports"
                                      : c.provider_key === "meta_ads"
                                      ? "View Meta Ads campaign insights"
                                      : c.provider_key === "supermetrics"
                                      ? "View Supermetrics marketing reports"
                                      : "View GA4 reports"
                                  }
                                >
                                  Reports
                                </button>
                              </>
                            )}
                            {!isViewer && (
                              <button
                                className="deletebtn"
                                onClick={() => {
                                  if (c.provider_key === "google_ads") disconnectGoogleAds(c);
                                  else if (c.provider_key === "meta_ads") disconnectMetaAds(c);
                                  else if (c.provider_key === "supermetrics") disconnectSupermetrics(c);
                                  else if (c.provider_key === "groq") disconnectGroq(c);
                                  else disconnectConnection(c);
                                }}
                                title="Disconnect integration"
                              >
                                <LogOut size={13} />
                              </button>
                            )}
                          </div>
                        </>
                      ) : (c.status === "needs_configuration" || c.status === "needs_customer_selection" || c.status === "needs_account_selection" || c.status === "needs_source_selection") ? (
                        <>
                          <span className="pending" style={{ color: "#d97706" }}>
                            <i /> {c.provider_key === "google_ads"
                              ? "Customer required"
                              : c.provider_key === "meta_ads"
                              ? "Ad account required"
                              : c.provider_key === "supermetrics"
                              ? (c.has_secret ? "Source required" : "API key required")
                              : c.provider_key === "groq"
                              ? "API key required"
                              : "Property required"}
                          </span>
                          <div className="action-group">
                            <button
                              className="primary smallbtn"
                              disabled={isViewer}
                              onClick={() => {
                                if (c.provider_key === "google_ads") openAdsCustomerModal(c);
                                else if (c.provider_key === "meta_ads") openMetaAccountModal(c);
                                else if (c.provider_key === "supermetrics") {
                                  if (c.has_secret) openSmSourceModal(c);
                                  else openSmConfigModal(c);
                                } else if (c.provider_key === "groq") openGroqConfigModal(c);
                                else openPropertyModal(c);
                              }}
                            >
                              {c.provider_key === "google_ads"
                                ? "Select customer"
                                : c.provider_key === "meta_ads"
                                ? "Select ad account"
                                : c.provider_key === "supermetrics"
                                ? (c.has_secret ? "Select data source" : "Configure API key")
                                : c.provider_key === "groq"
                                ? "Configure API key"
                                : "Select property"}
                            </button>
                            {!isViewer && (
                              <button
                                className="deletebtn"
                                onClick={() => removeConnection(c.id)}
                                title="Delete connection"
                              >
                                <Trash2 size={14} />
                              </button>
                            )}
                          </div>
                        </>
                      ) : (
                        <>
                          <span className="pending">
                            <i /> {c.provider_key === "supermetrics" || c.provider_key === "groq" ? "API key required" : "Authorization needed"}
                          </span>
                          <div className="action-group">
                            <button
                              className="secondary smallbtn"
                              disabled={isViewer}
                              onClick={() => {
                                if (c.provider_key === "google_ads") startGoogleAdsOAuth(c.id);
                                else if (c.provider_key === "meta_ads") startMetaAdsOAuth(c.id);
                                else if (c.provider_key === "supermetrics") openSmConfigModal(c);
                                else if (c.provider_key === "groq") openGroqConfigModal(c);
                                else startGoogleOAuth(c.id);
                              }}
                            >
                              {c.provider_key === "supermetrics" || c.provider_key === "groq" ? "Configure Key" : "Authorize"}
                            </button>
                            {!isViewer && (
                              <button
                                className="deletebtn"
                                onClick={() => removeConnection(c.id)}
                                title="Delete connection"
                              >
                                <Trash2 size={14} />
                              </button>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  ))}
                  {customs.map((c) => (
                    <div className="connectionrow" key={c.id}>
                      <div className="miniicon custommini">
                        <CodeIcon />
                      </div>
                      <div className="connectionname">
                        <b>{c.name}</b>
                        <small>{c.base_url}</small>
                      </div>
                      <span className="saved">
                        <Check size={12} /> Saved
                      </span>
                      {c.has_secret ? (
                        <span className="secret-badge" title="Encrypted credentials stored securely">
                          <ShieldCheck size={12} /> Stored credential
                        </span>
                      ) : (
                        <span className="secret-badge none" title="No credentials configured">
                          No credential
                        </span>
                      )}
                      {!isViewer && (
                        <button
                          className="deletebtn"
                          onClick={() => removeCustom(c.id)}
                          title="Delete custom connector"
                        >
                          <Trash2 size={14} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </section>
            <section className="panel tips">
              <div className="tipicon">
                <Sparkles size={18} />
              </div>
              <div className="tiplabel">QUICK TIP</div>
              <h3>One login. Your choice of data.</h3>
              <p>
                Signed in securely with Firebase. Connect platforms and APIs to
                this workspace without sharing master credentials.
              </p>
              <button
                className="learnbtn"
                onClick={() =>
                  setToast(
                    "Orbit Connector Hub uses Firebase Authentication with Google Sign-In and per-organization RBAC.",
                  )
                }
              >
                How authentication works <ArrowUpRight size={14} />
              </button>
            </section>
          </div>
          <footer>
            © 2026 Orbit Connector Hub{" "}
            <span>Built for clarity. Designed to scale.</span>
          </footer>
        </div>
      </section>
      {modal && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setModal(false);
          }}
        >
          <div className="modal">
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> CUSTOM INTEGRATION
                </div>
                <h2>Connect your own API</h2>
                <p>Use this for a provider we do not list yet.</p>
              </div>
              <button className="iconbtn" onClick={() => setModal(false)}>
                <X size={19} />
              </button>
            </div>
            <div className="stepnote">
              <span>01</span>
              <div>
                <b>Connection details</b>
                <small>Enter the API endpoint and how it is protected.</small>
              </div>
            </div>
            <label>
              Connector name
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="e.g. Shopify Analytics"
              />
            </label>
            <div className="formrow">
              <label>
                Base URL
                <input
                  value={form.base_url}
                  onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                  placeholder="https://api.example.com"
                />
              </label>
              <label>
                Endpoint path
                <input
                  value={form.endpoint_path}
                  onChange={(e) =>
                    setForm({ ...form, endpoint_path: e.target.value })
                  }
                  placeholder="/v1/status"
                />
              </label>
            </div>
            <label>
              Authentication
              <select
                value={form.auth_type}
                onChange={(e) =>
                  setForm({ ...form, auth_type: e.target.value })
                }
              >
                <option value="none">No authentication</option>
                <option value="bearer">Bearer token</option>
                <option value="api_key_header">API key in header</option>
                <option value="basic">Basic authentication</option>
              </select>
            </label>
            {form.auth_type !== "none" && (
              <div className="formrow">
                <label>
                  {form.auth_type === "basic"
                    ? "Username:password"
                    : "Secret / API key"}
                  <input
                    type="password"
                    value={form.secret}
                    onChange={(e) =>
                      setForm({ ...form, secret: e.target.value })
                    }
                    placeholder="Enter credential"
                  />
                </label>
                {form.auth_type === "api_key_header" && (
                  <label>
                    Header name
                    <input
                      value={form.secret_label}
                      onChange={(e) =>
                        setForm({ ...form, secret_label: e.target.value })
                      }
                      placeholder="X-API-Key"
                    />
                  </label>
                )}
              </div>
            )}
            <label>
              Additional headers <span className="optional">OPTIONAL · JSON</span>
              <textarea
                rows={2}
                value={form.headers}
                onChange={(e) => setForm({ ...form, headers: e.target.value })}
              />
            </label>
            {testResult && (
              <div
                className={`testresult ${testResult.success ? "ok" : "bad"}`}
              >
                <div>
                  <b>
                    {testResult.success
                      ? "Connection successful"
                      : "Connection test failed"}
                  </b>
                  <small>
                    {testResult.message}
                    {testResult.http_status
                      ? ` · HTTP ${testResult.http_status}`
                      : ""}
                  </small>
                </div>
                {testResult.preview && <pre>{testResult.preview}</pre>}
              </div>
            )}
            <div className="modalfoot">
              <span>
                <ShieldCheck size={14} /> Credentials are encrypted before saving.
              </span>
              <div>
                <button
                  className="secondary"
                  disabled={testing}
                  onClick={testCustom}
                >
                  {testing ? "Testing…" : "Test connection"}
                </button>
                <button
                  className="primary"
                  disabled={saving || !form.name.trim()}
                  onClick={saveCustom}
                >
                  {saving ? "Saving…" : "Save connector"}
                </button>
              </div>
            </div>
            <p className="securitynote">
              For safety, custom endpoints must use HTTPS and resolve to a public
              host. This starter tests a GET endpoint; data mapping and scheduled
              sync are configured in a later phase.
            </p>
          </div>
        </div>
      )}
      {propModalConn && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setPropModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 520 }}>
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> GOOGLE ANALYTICS 4
                </div>
                <h2>Select GA4 Property</h2>
                <p>Choose the target property for {propModalConn.display_name}.</p>
              </div>
              <button className="iconbtn" onClick={() => setPropModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingProps ? (
              <div style={{ padding: "20px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                Loading accessible GA4 properties…
              </div>
            ) : gaProperties.length === 0 ? (
              <div style={{ padding: "20px 0", textAlign: "center", color: "#a54b39", fontSize: 11 }}>
                No accessible GA4 properties found for this Google account.
              </div>
            ) : (
              <div style={{ margin: "10px 0" }}>
                <label>
                  Accessible Properties ({gaProperties.length})
                  <select
                    value={selectedPropId}
                    onChange={(e) => setSelectedPropId(e.target.value)}
                    style={{ marginTop: 6 }}
                  >
                    {gaProperties.map((p) => (
                      <option key={p.property_id} value={p.property_id}>
                        {p.display_name} ({p.property_id}) — {p.account_name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <ShieldCheck size={14} /> Only authorized properties can be synchronized.
              </span>
              <div>
                <button
                  className="secondary"
                  onClick={() => setPropModalConn(null)}
                >
                  Cancel
                </button>
                <button
                  className="primary"
                  disabled={savingProp || !selectedPropId || gaProperties.length === 0}
                  onClick={saveSelectedProperty}
                >
                  {savingProp ? "Saving…" : "Confirm property"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {reportModalConn && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setReportModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 680 }}>
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> GA4 REPORT DATA
                </div>
                <h2>{reportModalConn.display_name} Reporting</h2>
                <p>
                  Property: {reportModalConn.property_name || reportModalConn.property_id || "Connected"}
                </p>
              </div>
              <button className="iconbtn" onClick={() => setReportModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingReports ? (
              <div style={{ padding: "25px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                Loading reporting rows…
              </div>
            ) : gaReports.length === 0 ? (
              <div style={{ padding: "25px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                No reporting rows found. Trigger a sync job to ingest recent data.
              </div>
            ) : (
              <div>
                <div className="ga-summary">
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>TOTAL SESSIONS</span>
                    <div className="ga-sum-val">
                      {gaReports.reduce((acc, r) => acc + r.sessions, 0).toLocaleString()}
                    </div>
                  </div>
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>ACTIVE USERS</span>
                    <div className="ga-sum-val">
                      {gaReports.reduce((acc, r) => acc + r.active_users, 0).toLocaleString()}
                    </div>
                  </div>
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>TOTAL EVENTS</span>
                    <div className="ga-sum-val">
                      {gaReports.reduce((acc, r) => acc + r.event_count, 0).toLocaleString()}
                    </div>
                  </div>
                </div>
                <div style={{ maxHeight: 280, overflowY: "auto", border: "1px solid var(--line)", borderRadius: 7 }}>
                  <table className="ga-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Channel Group</th>
                        <th style={{ textAlign: "right" }}>Sessions</th>
                        <th style={{ textAlign: "right" }}>Active Users</th>
                        <th style={{ textAlign: "right" }}>Events</th>
                      </tr>
                    </thead>
                    <tbody>
                      {gaReports.map((r) => (
                        <tr key={r.id}>
                          <td>{r.report_date}</td>
                          <td><b>{r.channel_group}</b></td>
                          <td style={{ textAlign: "right" }}>{r.sessions.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.active_users.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.event_count.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <Check size={14} /> Normalized metrics from Google Analytics Data API
              </span>
              <button
                className="secondary"
                onClick={() => setReportModalConn(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
      {adsCustomerModalConn && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setAdsCustomerModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 520 }}>
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> GOOGLE ADS CONFIGURATION
                </div>
                <h2>Select Customer Account</h2>
                <p>Choose the target account for {adsCustomerModalConn.display_name}.</p>
              </div>
              <button className="iconbtn" onClick={() => setAdsCustomerModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingAdsCustomers ? (
              <div style={{ padding: "20px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                Loading accessible Google Ads accounts…
              </div>
            ) : adsCustomers.length === 0 ? (
              <div style={{ padding: "20px 0", textAlign: "center", color: "#a54b39", fontSize: 11 }}>
                No accessible Google Ads customer accounts found for this authenticated user.
              </div>
            ) : (
              <div style={{ margin: "10px 0" }}>
                <label>
                  Accessible Accounts ({adsCustomers.length})
                  <select
                    value={selectedAdsCustomerId}
                    onChange={(e) => setSelectedAdsCustomerId(e.target.value)}
                    style={{ marginTop: 6 }}
                  >
                    {adsCustomers.map((cust) => (
                      <option key={cust.customer_id} value={cust.customer_id}>
                        {cust.descriptive_name} ({cust.customer_id}) {cust.is_manager ? "— [Manager Account]" : ""}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <ShieldCheck size={14} /> Synced via Google Ads REST API v18.
              </span>
              <div>
                <button
                  className="secondary"
                  onClick={() => setAdsCustomerModalConn(null)}
                >
                  Cancel
                </button>
                <button
                  className="primary"
                  disabled={savingAdsCustomer || !selectedAdsCustomerId || adsCustomers.length === 0}
                  onClick={saveSelectedAdsCustomer}
                >
                  {savingAdsCustomer ? "Saving…" : "Confirm customer"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {adsReportModalConn && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setAdsReportModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 740 }}>
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> GOOGLE ADS CAMPAIGN DATA
                </div>
                <h2>{adsReportModalConn.display_name} Performance</h2>
                <p>
                  Customer: {adsReportModalConn.customer_name || adsReportModalConn.customer_id || "Connected"}
                </p>
              </div>
              <button className="iconbtn" onClick={() => setAdsReportModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingAdsReports ? (
              <div style={{ padding: "25px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                Loading campaign reporting rows…
              </div>
            ) : adsReports.length === 0 ? (
              <div style={{ padding: "25px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                No campaign rows found. Trigger a sync job to ingest recent performance.
              </div>
            ) : (
              <div>
                {adsReportSummary && (
                  <div className="ga-summary" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>IMPRESSIONS</span>
                      <div className="ga-sum-val">
                        {adsReportSummary.total_impressions.toLocaleString()}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CLICKS</span>
                      <div className="ga-sum-val">
                        {adsReportSummary.total_clicks.toLocaleString()}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>TOTAL SPEND ({adsReportSummary.currency_code || "USD"})</span>
                      <div className="ga-sum-val">
                        {Number(adsReportSummary.total_cost).toFixed(2)}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CONVERSIONS</span>
                      <div className="ga-sum-val">
                        {adsReportSummary.total_conversions.toLocaleString()}
                      </div>
                    </div>
                  </div>
                )}
                <div style={{ maxHeight: 280, overflowY: "auto", border: "1px solid var(--line)", borderRadius: 7 }}>
                  <table className="ga-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Campaign</th>
                        <th>Status</th>
                        <th style={{ textAlign: "right" }}>Impr.</th>
                        <th style={{ textAlign: "right" }}>Clicks</th>
                        <th style={{ textAlign: "right" }}>Cost ({adsReportSummary?.currency_code || "USD"})</th>
                        <th style={{ textAlign: "right" }}>Conv.</th>
                      </tr>
                    </thead>
                    <tbody>
                      {adsReports.map((r) => (
                        <tr key={r.id}>
                          <td>{r.report_date}</td>
                          <td><b>{r.campaign_name}</b></td>
                          <td>
                            <span style={{
                              fontSize: 9,
                              fontWeight: 700,
                              padding: "2px 6px",
                              borderRadius: 4,
                              background: r.campaign_status === "ENABLED" ? "rgba(16,185,129,0.15)" : "rgba(156,163,175,0.2)",
                              color: r.campaign_status === "ENABLED" ? "#059669" : "#6b7280",
                            }}>
                              {r.campaign_status}
                            </span>
                          </td>
                          <td style={{ textAlign: "right" }}>{r.impressions.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.clicks.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{Number(r.cost).toFixed(2)}</td>
                          <td style={{ textAlign: "right" }}>{r.conversions.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <Check size={14} /> Normalized GAQL campaign performance metrics
              </span>
              <button
                className="secondary"
                onClick={() => setAdsReportModalConn(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
      {metaAccountModalConn && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setMetaAccountModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 520 }}>
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> META ADS CONFIGURATION
                </div>
                <h2>Select Meta Ad Account</h2>
                <p>Choose the target ad account for {metaAccountModalConn.display_name}.</p>
              </div>
              <button className="iconbtn" onClick={() => setMetaAccountModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingMetaAccounts ? (
              <div style={{ padding: "20px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                Loading accessible Meta ad accounts…
              </div>
            ) : metaAccounts.length === 0 ? (
              <div style={{ padding: "20px 0", textAlign: "center", color: "#a54b39", fontSize: 11 }}>
                No accessible Meta ad accounts found for this authenticated user.
              </div>
            ) : (
              <div style={{ margin: "10px 0" }}>
                <label>
                  Accessible Ad Accounts ({metaAccounts.length})
                  <select
                    value={selectedMetaAccountId}
                    onChange={(e) => setSelectedMetaAccountId(e.target.value)}
                    style={{ marginTop: 6 }}
                  >
                    {metaAccounts.map((acct) => (
                      <option key={acct.account_id} value={acct.account_id}>
                        {acct.name} (act_{acct.account_id}) {acct.currency ? `[${acct.currency}]` : ""}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <ShieldCheck size={14} /> Synced via Meta Marketing API v27.0.
              </span>
              <div>
                <button
                  className="secondary"
                  onClick={() => setMetaAccountModalConn(null)}
                >
                  Cancel
                </button>
                <button
                  className="primary"
                  disabled={savingMetaAccount || !selectedMetaAccountId || metaAccounts.length === 0}
                  onClick={saveSelectedMetaAccount}
                >
                  {savingMetaAccount ? "Saving…" : "Confirm account"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {metaReportModalConn && (
        <div
          className="overlay"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setMetaReportModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 840 }}>
            <div className="modalhead">
              <div>
                <div className="eyebrow">
                  <span className="eyebrowline" /> META ADS CAMPAIGN INSIGHTS
                </div>
                <h2>{metaReportModalConn.display_name} Performance</h2>
                <p>
                  Ad Account: {metaReportModalConn.ad_account_name || (metaReportModalConn.ad_account_id ? `act_${metaReportModalConn.ad_account_id}` : "Connected")}
                </p>
              </div>
              <button className="iconbtn" onClick={() => setMetaReportModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingMetaReports ? (
              <div style={{ padding: "25px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                Loading campaign reporting rows…
              </div>
            ) : metaReports.length === 0 ? (
              <div style={{ padding: "25px 0", textAlign: "center", color: "#666", fontSize: 11 }}>
                No campaign rows found. Trigger a sync job to ingest recent performance.
              </div>
            ) : (
              <div>
                {metaReportSummary && (
                  <div className="ga-summary" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>IMPRESSIONS</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.total_impressions.toLocaleString()}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>REACH</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.total_reach.toLocaleString()}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CLICKS</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.total_clicks.toLocaleString()}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>TOTAL SPEND {metaReportSummary.currency ? `(${metaReportSummary.currency})` : ""}</span>
                      <div className="ga-sum-val">
                        {Number(metaReportSummary.total_spend).toFixed(2)}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CONVERSIONS</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.total_conversions.toLocaleString()}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>AVG CTR</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.average_ctr != null ? `${Number(metaReportSummary.average_ctr).toFixed(2)}%` : "—"}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>AVG CPC {metaReportSummary.currency ? `(${metaReportSummary.currency})` : ""}</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.average_cpc != null ? Number(metaReportSummary.average_cpc).toFixed(2) : "—"}
                      </div>
                    </div>
                    <div className="ga-sum-card">
                      <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>AVG FREQUENCY</span>
                      <div className="ga-sum-val">
                        {metaReportSummary.average_frequency != null ? Number(metaReportSummary.average_frequency).toFixed(2) : "—"}
                      </div>
                    </div>
                  </div>
                )}
                <div style={{ maxHeight: 280, overflowY: "auto", border: "1px solid var(--line)", borderRadius: 7 }}>
                  <table className="ga-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Campaign</th>
                        <th>Objective</th>
                        <th style={{ textAlign: "right" }}>Impr.</th>
                        <th style={{ textAlign: "right" }}>Reach</th>
                        <th style={{ textAlign: "right" }}>Clicks</th>
                        <th style={{ textAlign: "right" }}>Spend {metaReportSummary?.currency ? `(${metaReportSummary.currency})` : ""}</th>
                        <th style={{ textAlign: "right" }}>CTR</th>
                        <th style={{ textAlign: "right" }}>Conv.</th>
                      </tr>
                    </thead>
                    <tbody>
                      {metaReports.map((r) => (
                        <tr key={r.id}>
                          <td>{r.report_date}</td>
                          <td><b>{r.campaign_name}</b></td>
                          <td>
                            <span style={{
                              fontSize: 9,
                              fontWeight: 700,
                              padding: "2px 6px",
                              borderRadius: 4,
                              background: "rgba(59,130,246,0.15)",
                              color: "#2563eb",
                            }}>
                              {r.objective || "CAMPAIGN"}
                            </span>
                          </td>
                          <td style={{ textAlign: "right" }}>{r.impressions.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.reach.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.clicks.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{Number(r.spend).toFixed(2)}</td>
                          <td style={{ textAlign: "right" }}>{r.ctr != null ? `${Number(r.ctr).toFixed(2)}%` : "—"}</td>
                          <td style={{ textAlign: "right" }}>{r.conversions.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <Check size={14} /> Normalized Meta Marketing API v27.0 insights metrics
              </span>
              <button
                className="secondary"
                onClick={() => setMetaReportModalConn(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
      {smConfigModalConn && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setSmConfigModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 520 }}>
            <div className="modalhead">
              <div>
                <h3>Configure Supermetrics API Key</h3>
                <p>Enterprise v2 Authentication</p>
              </div>
              <button className="iconbtn" onClick={() => setSmConfigModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            <div className="modalbody" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <p style={{ fontSize: 13, color: "var(--muted)", margin: 0 }}>
                Enter your Supermetrics API key. It will be verified against the official API and stored encrypted at rest.
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, fontWeight: 600 }}>Supermetrics API Key</label>
                <input
                  type="password"
                  placeholder="sm_..."
                  value={smApiKey}
                  onChange={(e) => setSmApiKey(e.target.value)}
                  style={{
                    padding: "9px 12px",
                    borderRadius: 6,
                    border: "1px solid var(--line)",
                    background: "var(--bg)",
                    color: "inherit",
                    fontSize: 13,
                  }}
                />
              </div>
            </div>
            <div className="modalfoot">
              <button className="secondary" onClick={() => setSmConfigModalConn(null)}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={savingSmConfig || !smApiKey.trim()}
                onClick={saveSmConfig}
              >
                {savingSmConfig ? "Verifying…" : "Validate & Save"}
              </button>
            </div>
          </div>
        </div>
      )}
      {smSourceModalConn && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setSmSourceModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 540 }}>
            <div className="modalhead">
              <div>
                <h3>Select Supermetrics Data Source</h3>
                <p>Choose marketing platform & accounts</p>
              </div>
              <button className="iconbtn" onClick={() => setSmSourceModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingSmSources ? (
              <div style={{ padding: "30px 0", textAlign: "center", color: "#666", fontSize: 12 }}>
                Loading available data sources…
              </div>
            ) : (
              <div className="modalbody" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Data Source</label>
                  <select
                    value={selectedSmDsId}
                    onChange={(e) => setSelectedSmDsId(e.target.value)}
                    style={{
                      padding: "9px 12px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--bg)",
                      color: "inherit",
                      fontSize: 13,
                    }}
                  >
                    {smSources.map((s) => (
                      <option key={s.ds_id} value={s.ds_id}>
                        {s.name} ({s.ds_id}) {s.category ? `· ${s.category}` : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>
                    Target Account IDs <span style={{ fontWeight: 400, color: "var(--muted)" }}>(Optional, comma-separated)</span>
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. 123-456-7890, 987-654-3210"
                    value={smAccountsInput}
                    onChange={(e) => setSmAccountsInput(e.target.value)}
                    style={{
                      padding: "9px 12px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--bg)",
                      color: "inherit",
                      fontSize: 13,
                    }}
                  />
                  <small style={{ color: "var(--muted)", fontSize: 11 }}>
                    If empty, all authorized accounts for this data source will be queried.
                  </small>
                </div>
              </div>
            )}
            <div className="modalfoot">
              <button className="secondary" onClick={() => setSmSourceModalConn(null)}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={savingSmSource || !selectedSmDsId}
                onClick={saveSmSource}
              >
                {savingSmSource ? "Saving…" : "Save Selection"}
              </button>
            </div>
          </div>
        </div>
      )}
      {smReportModalConn && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setSmReportModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 940 }}>
            <div className="modalhead">
              <div>
                <h3>Supermetrics Marketing Reports</h3>
                <p>
                  Source: {smReportModalConn.ds_name || smReportModalConn.ds_id || "Connected Data Source"}
                  {smReportModalConn.ds_accounts ? ` · Accounts: ${smReportModalConn.ds_accounts}` : ""}
                </p>
              </div>
              <button className="iconbtn" onClick={() => setSmReportModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            {loadingSmReports ? (
              <div style={{ padding: "30px 0", textAlign: "center", color: "#666", fontSize: 12 }}>
                Loading Supermetrics reports…
              </div>
            ) : smReports.length === 0 ? (
              <div style={{ padding: "30px 0", textAlign: "center", color: "#666", fontSize: 12 }}>
                No reporting rows found. Trigger a sync job to ingest recent marketing data.
              </div>
            ) : (
              <div>
                <div className="ga-summary">
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>IMPRESSIONS</span>
                    <div className="ga-sum-val">
                      {(smReportSummary?.total_impressions || 0).toLocaleString()}
                    </div>
                  </div>
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CLICKS</span>
                    <div className="ga-sum-val">
                      {(smReportSummary?.total_clicks || 0).toLocaleString()}
                    </div>
                  </div>
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>TOTAL COST</span>
                    <div className="ga-sum-val">
                      ${Number(smReportSummary?.total_cost || 0).toFixed(2)}
                    </div>
                  </div>
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CONVERSIONS</span>
                    <div className="ga-sum-val">
                      {(smReportSummary?.total_conversions || 0).toLocaleString()}
                    </div>
                  </div>
                  <div className="ga-sum-card">
                    <span style={{ fontSize: 9, color: "#666", fontWeight: 700 }}>CONV. VALUE</span>
                    <div className="ga-sum-val">
                      ${Number(smReportSummary?.total_conversions_value || 0).toFixed(2)}
                    </div>
                  </div>
                </div>
                <div style={{ maxHeight: 280, overflowY: "auto", border: "1px solid var(--line)", borderRadius: 7 }}>
                  <table className="ga-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Source</th>
                        <th>Campaign</th>
                        <th>Channel</th>
                        <th style={{ textAlign: "right" }}>Impr.</th>
                        <th style={{ textAlign: "right" }}>Clicks</th>
                        <th style={{ textAlign: "right" }}>Cost</th>
                        <th style={{ textAlign: "right" }}>Conv.</th>
                        <th style={{ textAlign: "right" }}>Value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {smReports.map((r) => (
                        <tr key={r.id}>
                          <td>{r.report_date}</td>
                          <td>
                            <span style={{
                              fontSize: 9,
                              fontWeight: 700,
                              padding: "2px 6px",
                              borderRadius: 4,
                              background: "rgba(16,185,129,0.15)",
                              color: "#059669",
                            }}>
                              {r.ds_id}
                            </span>
                          </td>
                          <td><b>{r.campaign_name || "—"}</b></td>
                          <td>{r.channel_group || "—"}</td>
                          <td style={{ textAlign: "right" }}>{r.impressions.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.clicks.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>${Number(r.cost).toFixed(2)}</td>
                          <td style={{ textAlign: "right" }}>{r.conversions.toLocaleString()}</td>
                          <td style={{ textAlign: "right" }}>{r.conversions_value != null ? `$${Number(r.conversions_value).toFixed(2)}` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            <div className="modalfoot">
              <span>
                <Check size={14} /> Normalized Supermetrics Enterprise v2 marketing reports
              </span>
              <button className="secondary" onClick={() => setSmReportModalConn(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
      {groqConfigModalConn && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setGroqConfigModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 540 }}>
            <div className="modalhead">
              <div>
                <h3>Configure Groq LPU Integration</h3>
                <p>High-speed LLM inference configuration</p>
              </div>
              <button className="iconbtn" onClick={() => setGroqConfigModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            <div className="modalbody" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, fontWeight: 600 }}>Groq API Key</label>
                <input
                  type="password"
                  placeholder="gsk_..."
                  value={groqApiKey}
                  onChange={(e) => setGroqApiKey(e.target.value)}
                  style={{
                    padding: "9px 12px",
                    borderRadius: 6,
                    border: "1px solid var(--line)",
                    background: "var(--bg)",
                    color: "inherit",
                    fontSize: 13,
                  }}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, fontWeight: 600 }}>Default Model</label>
                <select
                  value={groqSelectedModel}
                  onChange={(e) => setGroqSelectedModel(e.target.value)}
                  style={{
                    padding: "9px 12px",
                    borderRadius: 6,
                    border: "1px solid var(--line)",
                    background: "var(--bg)",
                    color: "inherit",
                    fontSize: 13,
                  }}
                >
                  <option value="llama-3.3-70b-versatile">llama-3.3-70b-versatile (Recommended)</option>
                  <option value="llama-3.1-8b-instant">llama-3.1-8b-instant (Fastest)</option>
                  <option value="llama-3.1-70b-versatile">llama-3.1-70b-versatile</option>
                  <option value="mixtral-8x7b-32768">mixtral-8x7b-32768</option>
                  <option value="gemma2-9b-it">gemma2-9b-it</option>
                  {groqModels
                    .filter((m) => !["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "llama-3.1-70b-versatile", "mixtral-8x7b-32768", "gemma2-9b-it"].includes(m.id))
                    .map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name || m.id}
                      </option>
                    ))}
                </select>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Temperature ({groqTemperature})</label>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={groqTemperature}
                    onChange={(e) => setGroqTemperature(parseFloat(e.target.value))}
                  />
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <label style={{ fontSize: 12, fontWeight: 600 }}>Max Tokens</label>
                  <input
                    type="number"
                    min="1"
                    max="8192"
                    value={groqMaxTokens}
                    onChange={(e) => setGroqMaxTokens(parseInt(e.target.value, 10) || 256)}
                    style={{
                      padding: "8px 10px",
                      borderRadius: 6,
                      border: "1px solid var(--line)",
                      background: "var(--bg)",
                      color: "inherit",
                      fontSize: 13,
                    }}
                  />
                </div>
              </div>
            </div>
            <div className="modalfoot">
              <button className="secondary" onClick={() => setGroqConfigModalConn(null)}>
                Cancel
              </button>
              <button
                className="primary"
                disabled={savingGroqConfig || !groqApiKey.trim()}
                onClick={saveGroqConfig}
              >
                {savingGroqConfig ? "Validating…" : "Validate & Save"}
              </button>
            </div>
          </div>
        </div>
      )}
      {groqTestModalConn && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setGroqTestModalConn(null);
          }}
        >
          <div className="modal" style={{ maxWidth: 640 }}>
            <div className="modalhead">
              <div>
                <h3>Test Groq LLM Inference</h3>
                <p>Model: {groqTestModalConn.selected_model || "llama-3.3-70b-versatile"}</p>
              </div>
              <button className="iconbtn" onClick={() => setGroqTestModalConn(null)}>
                <X size={19} />
              </button>
            </div>
            <div className="modalbody" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, fontWeight: 600 }}>Test Prompt</label>
                <textarea
                  rows={3}
                  value={groqPrompt}
                  onChange={(e) => setGroqPrompt(e.target.value)}
                  placeholder="Enter a prompt to test inference..."
                  style={{
                    padding: "9px 12px",
                    borderRadius: 6,
                    border: "1px solid var(--line)",
                    background: "var(--bg)",
                    color: "inherit",
                    fontSize: 13,
                    resize: "vertical",
                  }}
                />
              </div>
              {groqResult && (
                <div style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  padding: "12px",
                  borderRadius: 6,
                  border: "1px solid var(--line)",
                  background: "var(--panel)",
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: "var(--muted)" }}>COMPLETION</span>
                    <span style={{ fontSize: 10, color: "#059669", background: "rgba(16,185,129,0.15)", padding: "2px 6px", borderRadius: 4 }}>
                      {groqResult.total_tokens} tokens ({groqResult.prompt_tokens} in / {groqResult.completion_tokens} out)
                    </span>
                  </div>
                  <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, whiteSpace: "pre-wrap" }}>
                    {groqResult.content}
                  </p>
                </div>
              )}
            </div>
            <div className="modalfoot">
              <button className="secondary" onClick={() => setGroqTestModalConn(null)}>
                Close
              </button>
              <button
                className="primary"
                disabled={runningGroqTest || !groqPrompt.trim()}
                onClick={runGroqCompletionTest}
              >
                {runningGroqTest ? "Inferring…" : "Run Completion"}
              </button>
            </div>
          </div>
        </div>
      )}
      {toast && (
        <div className="toast">
          <span>{toast}</span>
          <button onClick={() => setToast("")}>
            <X size={15} />
          </button>
        </div>
      )}
    </main>
  );
}

function CodeIcon() {
  return <span style={{ fontWeight: 800, fontSize: 16 }}>{"{ }"}</span>;
}
