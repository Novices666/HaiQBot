/** 面板 v2 用到的后端响应形状（与 webui/api/*.py 一一对应，字段名以 Python 侧为准）。 */

export type AuditRow = {
  id?: number;
  ts?: number;
  action: string;
  plugin_id?: string | null;
  actor?: string | null;
  detail?: unknown;
  [key: string]: unknown;
};

export type PlatformInfo = {
  name?: string;
  bot_id?: string | null;
  health?: { connected?: boolean };
  [key: string]: unknown;
};

export type Overview = {
  runtime: {
    started_at: number;
    uptime_seconds: number;
    events_total: number;
    handled_total: number;
    last_event_ts: number | null;
  };
  health: Record<string, unknown>;
  platforms: Record<string, PlatformInfo>;
  plugins: { total: number; loaded: number; blocked: string[]; errors: Record<string, string> };
  media: { count: number };
  outbound: { min_interval_seconds: number | null; global_per_minute: number | null } | null;
  config: {
    onebot_mode: string | null;
    onebot_enabled: boolean;
    official_enabled: boolean;
    quota_enabled: boolean;
    log_level: string;
    write_enabled: boolean;
  };
  recent_audit: AuditRow[];
};

export type SchemaField = {
  key: string;
  label?: string;
  kind?: string;
  default?: unknown;
  choices?: string[];
  description?: string;
  [key: string]: unknown;
};

export type SecretSlot = {
  slot: string;
  label: string;
  ref: string;
  required: boolean;
  set: boolean;
  group: string;
};

export type CommandRow = {
  plugin: string;
  plugin_name?: string;
  command: string;
  aliases?: string[];
  usage?: string;
  description?: string;
  min_role?: number;
};

export type PluginRow = {
  id: string;
  name?: string;
  version?: string;
  description?: string;
  source?: string;
  enabled: boolean;
  loaded: boolean;
  blocked?: boolean;
  error?: string | null;
  rules?: string[];
  commands?: CommandRow[];
  config?: Record<string, unknown>;
  config_schema?: SchemaField[];
  secrets?: SecretSlot[];
  capabilities?: string[];
  provides?: Record<string, unknown>;
  requires?: string[];
};

export type ConfigField = {
  path: string;
  section: string;
  key: string;
  kind: string;
  label: string;
  group: string;
  group_label: string;
  apply: string;
  apply_label: string;
  minimum: number | null;
  maximum: number | null;
  choices: string[];
  readonly: boolean;
  help: string;
  value: unknown;
};

export type ConfigPayload = {
  write_enabled: boolean;
  config_path: string | null;
  fields: ConfigField[];
  secrets: Record<string, unknown>[];
  raw: string;
  path: string;
};

export type HistoryRow = { id: string; ts: number; size: number };

export type MatchedRule = {
  plugin: string;
  rule: string;
  priority: number;
  commands: string[];
  usage: string;
  block: boolean;
  params: Record<string, string>;
};

export type SkippedRule = { plugin: string; rule: string; priority: number; reason: string };

export type ParseResult = {
  dry_run: boolean;
  note: string;
  event: {
    text: string;
    platform: string;
    channel_kind: string;
    channel_id: string;
    role: string;
    at_bot: boolean;
  };
  matched: MatchedRule[];
  skipped: SkippedRule[];
  rules_total: number;
};

export type LogRow = {
  seq: number;
  ts: number;
  level: string;
  [key: string]: unknown;
};

export type LogPayload = {
  items: LogRow[];
  cursor: { seq: number; dropped: number; size: number };
};

export type MediaRow = {
  id: string;
  url: string;
  plugin_id?: string | null;
  kind?: string | null;
  mime?: string | null;
  size?: number | null;
  tags?: unknown;
  ts?: number;
  [key: string]: unknown;
};

export type ChannelRow = {
  channel: string;
  enabled: number | boolean;
  plugins: string[];
  quota_user?: number;
  quota_chan?: number;
  extra_admins?: string[];
  note?: string;
  updated_at?: number;
};

export type InvocationRow = {
  id?: number;
  ts?: number;
  plugin_id?: string | null;
  capability?: string | null;
  ok?: number | boolean | null;
  ms?: number | null;
  error?: string | null;
  [key: string]: unknown;
};

/** `/api/services` 的单项：运行时真实注册的能力提供者。 */
export type ServiceInfo = {
  owner?: string | null;
  spec?: unknown;
};

/** `/api/invocations/stats` 的形状：按「能力 + 状态」聚合。 */
export type InvocationStat = {
  capability: string | null;
  status: string | null;
  n: number;
  avg_ms: number | null;
  max_ms: number | null;
};
