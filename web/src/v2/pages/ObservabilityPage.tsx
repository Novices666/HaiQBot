/** 日志 · 审计 · 媒体：日志按 seq 增量拉取，审计与媒体按需刷新。 */
import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Flex,
  Image,
  Input,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { del, get } from "../../api";
import { fmtTime, text, usePoll } from "../lib";
import type { AuditRow, InvocationRow, LogPayload, LogRow, MediaRow } from "../types";

const LEVEL_COLOR: Record<string, string> = {
  debug: "default",
  info: "blue",
  warning: "orange",
  warn: "orange",
  error: "red",
  critical: "magenta",
};

function LogsTab() {
  const [rows, setRows] = useState<LogRow[]>([]);
  const [level, setLevel] = useState<string>("");
  const [size, setSize] = useState(0);
  const [dropped, setDropped] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef(0);

  useEffect(() => {
    cursor.current = 0;
    setRows([]);
  }, [level]);

  useEffect(() => {
    let alive = true;
    let source: EventSource | null = null;

    const onFrame = (raw: string) => {
      try {
        const payload = JSON.parse(raw) as { items: LogRow[] };
        const incoming = payload.items ?? [];
        if (incoming.length === 0) return;
        cursor.current = Math.max(cursor.current, incoming[incoming.length - 1].seq);
        setRows((prev) => {
          const last = prev.length > 0 ? prev[prev.length - 1].seq : 0;
          const fresh = incoming.filter((row) => row.seq > last);
          return fresh.length > 0 ? [...prev, ...fresh].slice(-600) : prev;
        });
        setError(null);
      } catch {
        /* 心跳或半帧，忽略 */
      }
    };

    const start = async () => {
      // 先补一次历史：SSE 只推「连上之后」的新行
      try {
        const query = new URLSearchParams({ since: "0", limit: "200" });
        if (level) query.set("level", level);
        const payload = await get<LogPayload>(`/api/logs?${query.toString()}`);
        if (!alive) return;
        cursor.current = payload.cursor.seq;
        setSize(payload.cursor.size);
        setDropped(payload.cursor.dropped);
        setRows(payload.items);
      } catch (exc) {
        if (alive) setError((exc as Error).message);
        return;
      }
      if (!alive) return;

      const query = new URLSearchParams({ since: String(cursor.current) });
      if (level) query.set("level", level);
      source = new EventSource(`/api/logs/stream?${query.toString()}`);
      source.onmessage = (event) => onFrame(event.data);
      source.onerror = () => {
        // EventSource 自带重连，这里只提示，不自己再建连接（否则会叠加）
        if (alive) setError("实时连接断开，正在自动重连…");
      };
    };

    void start();

    return () => {
      alive = false;
      source?.close();
    };
  }, [level]);

  return (
    <Flex vertical gap={8}>
      <Flex align="center" gap={12}>
        <Select
          size="small"
          style={{ width: 140 }}
          value={level}
          onChange={setLevel}
          options={[
            { value: "", label: "全部级别" },
            { value: "info", label: "info 及以上" },
            { value: "warning", label: "warning 及以上" },
            { value: "error", label: "error 及以上" },
          ]}
        />
        <Button size="small" onClick={() => setRows([])}>
          清屏
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          环形缓冲 {size} 条 · 已丢弃 {dropped} 条 · 每 2.5 秒增量拉取
        </Typography.Text>
      </Flex>
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <div
        style={{
          maxHeight: 560,
          overflow: "auto",
          fontFamily: "JetBrains Mono, Consolas, monospace",
          fontSize: 12,
          lineHeight: 1.65,
        }}
      >
        {rows.length === 0 ? (
          <Typography.Text type="secondary">还没有日志。</Typography.Text>
        ) : (
          rows.map((row) => (
            <div key={row.seq} style={{ display: "flex", gap: 8 }}>
              <span style={{ opacity: 0.55, flex: "0 0 78px" }}>
                {new Date(row.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false })}
              </span>
              <Tag
                color={LEVEL_COLOR[row.level] ?? "default"}
                style={{ marginInlineEnd: 0, flex: "0 0 74px", textAlign: "center" }}
              >
                {row.level}
              </Tag>
              <span style={{ wordBreak: "break-all" }}>{text(row.event)}</span>
            </div>
          ))
        )}
      </div>
    </Flex>
  );
}

function AuditTab() {
  const [limit, setLimit] = useState(200);
  const audit = usePoll<AuditRow[]>(`/api/audit?limit=${limit}`);
  return (
    <Flex vertical gap={8}>
      <Flex align="center" gap={8}>
        <Typography.Text type="secondary">条数</Typography.Text>
        <Select
          size="small"
          style={{ width: 110 }}
          value={limit}
          onChange={setLimit}
          options={[100, 200, 500].map((n) => ({ value: n, label: String(n) }))}
        />
        <Button size="small" onClick={() => audit.reload()}>
          刷新
        </Button>
      </Flex>
      {audit.error ? <Alert type="error" showIcon message={audit.error.message} /> : null}
      <Table<AuditRow>
        size="small"
        rowKey={(row) => String(row.id ?? `${row.ts}:${row.action}`)}
        loading={!audit.data && !audit.error}
        dataSource={audit.data ?? []}
        pagination={{ pageSize: 15, hideOnSinglePage: true }}
        columns={[
          { title: "时间", dataIndex: "ts", width: 172, render: (v: number) => fmtTime(v) },
          { title: "动作", dataIndex: "action", width: 200 },
          { title: "插件", dataIndex: "plugin_id", width: 120, render: (v: unknown) => text(v) },
          { title: "操作者", dataIndex: "actor", width: 140, render: (v: unknown) => text(v) },
          {
            title: "详情",
            dataIndex: "detail",
            render: (v: unknown) => (
              <Typography.Text style={{ fontFamily: "monospace", fontSize: 12 }}>{text(v)}</Typography.Text>
            ),
          },
        ]}
      />
    </Flex>
  );
}

function MediaTab() {
  const [limit, setLimit] = useState(60);
  const media = usePoll<MediaRow[]>(`/api/media?limit=${limit}`);

  const remove = async (row: MediaRow) => {
    try {
      await del(`/api/media/${row.id}`);
      message.success("已删除");
      media.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    }
  };

  return (
    <Flex vertical gap={8}>
      <Space>
        <Select
          size="small"
          style={{ width: 110 }}
          value={limit}
          onChange={setLimit}
          options={[30, 60, 120].map((n) => ({ value: n, label: `${n} 条` }))}
        />
        <Button size="small" onClick={() => media.reload()}>
          刷新
        </Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          缩略图走 /i/&lt;id&gt;（内核保留的稳定 URL），点开看原图
        </Typography.Text>
      </Space>
      {media.error ? <Alert type="error" showIcon message={media.error.message} /> : null}
      <Flex wrap gap={12}>
        {(media.data ?? []).map((row) => (
          <Card
            key={row.id}
            size="small"
            style={{ width: 208 }}
            cover={
              <div style={{ height: 132, overflow: "hidden", background: "rgba(0,0,0,0.03)" }}>
                <Image src={row.url} height={132} width={208} style={{ objectFit: "cover" }} />
              </div>
            }
            actions={[
              <Button key="del" size="small" type="text" danger onClick={() => void remove(row)}>
                删除
              </Button>,
            ]}
          >
            <Typography.Text style={{ fontSize: 12 }} code>
              {row.plugin_id ?? "—"}
            </Typography.Text>
            <div style={{ fontSize: 11, opacity: 0.65 }}>{fmtTime(row.ts)}</div>
          </Card>
        ))}
      </Flex>
      {(media.data ?? []).length === 0 && !media.error ? (
        <Typography.Text type="secondary">媒体库还是空的。</Typography.Text>
      ) : null}
    </Flex>
  );
}

function InvocationTab() {
  const [filter, setFilter] = useState("");
  const path = filter ? `/api/invocations?limit=200&plugin=${encodeURIComponent(filter)}` : "/api/invocations?limit=200";
  const rows = usePoll<InvocationRow[]>(path);
  return (
    <Flex vertical gap={8}>
      <Space>
        <Input
          size="small"
          style={{ width: 200 }}
          placeholder="按插件 ID 过滤"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          allowClear
        />
        <Button size="small" onClick={() => rows.reload()}>
          刷新
        </Button>
      </Space>
      {rows.error ? <Alert type="error" showIcon message={rows.error.message} /> : null}
      <Table<InvocationRow>
        size="small"
        rowKey={(row) => String(row.id ?? `${row.ts}:${row.capability}`)}
        loading={!rows.data && !rows.error}
        dataSource={rows.data ?? []}
        pagination={{ pageSize: 15, hideOnSinglePage: true }}
        columns={[
          { title: "时间", dataIndex: "ts", width: 172, render: (v: number) => fmtTime(v) },
          { title: "插件", dataIndex: "plugin_id", width: 130, render: (v: unknown) => text(v) },
          { title: "能力", dataIndex: "capability", width: 160, render: (v: unknown) => text(v) },
          {
            title: "结果",
            dataIndex: "ok",
            width: 90,
            render: (v: unknown) =>
              v === 1 || v === true ? <Tag color="green">成功</Tag> : <Tag color="red">失败</Tag>,
          },
          { title: "耗时", dataIndex: "ms", width: 90, render: (v: unknown) => (v ? `${text(v)} ms` : "—") },
          { title: "错误", dataIndex: "error", render: (v: unknown) => text(v, "") },
        ]}
      />
    </Flex>
  );
}

export default function ObservabilityPage() {
  return (
    <Card size="small">
      <Tabs
        size="small"
        items={[
          { key: "logs", label: "日志", children: <LogsTab /> },
          { key: "audit", label: "审计", children: <AuditTab /> },
          { key: "media", label: "媒体", children: <MediaTab /> },
          { key: "invocations", label: "调用流水", children: <InvocationTab /> },
        ]}
      />
    </Card>
  );
}