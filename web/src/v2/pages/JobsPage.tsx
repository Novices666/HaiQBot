/** 定时任务：看谁在什么时候发什么，以及为什么没发。
 *
 * 动作只有「发文本」和空操作 —— 这里刻意不提供「执行任意命令」。
 */
import { useMemo, useState } from "react";
import { Alert, Button, Card, Flex, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag, Typography, message } from "antd";
import { del, post } from "../../api";
import { fmtTime, usePoll } from "../lib";

type JobRow = {
  id: string;
  name: string;
  kind: string;
  schedule: { seconds?: number; at?: string };
  action: string;
  params: { channel?: string; text?: string };
  enabled: boolean;
  next_run_at: number | null;
  last_run_at: number | null;
  last_status: string | null;
  last_error: string | null;
  due_in: number | null;
};

type JobsPayload = {
  items: JobRow[];
  channels: { channel: string; note: string }[];
  kinds: string[];
  actions: string[];
};

type Draft = {
  id?: string;
  name: string;
  kind: "interval" | "daily";
  seconds: number;
  at: string;
  channel: string;
  text: string;
  enabled: boolean;
};

const EMPTY: Draft = {
  name: "",
  kind: "daily",
  seconds: 3600,
  at: "09:30",
  channel: "",
  text: "",
  enabled: true,
};

function scheduleText(row: JobRow): string {
  if (row.kind === "interval") return `每 ${Math.round(Number(row.schedule?.seconds ?? 0))} 秒`;
  return `每天 ${row.schedule?.at ?? "?"}`;
}

function dueText(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds <= 0) return "即将执行";
  if (seconds < 60) return `${Math.round(seconds)} 秒后`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟后`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时后`;
  return `${(seconds / 86400).toFixed(1)} 天后`;
}

export default function JobsPage() {
  const jobs = usePoll<JobsPayload>("/api/jobs", 15000);
  const rows = useMemo(() => jobs.data?.items ?? [], [jobs.data]);
  const channels = jobs.data?.channels ?? [];

  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const channelOptions = channels.map((item) => ({
    value: item.channel,
    label: item.note ? `${item.channel}（${item.note}）` : item.channel,
  }));

  const openNew = () =>
    setDraft({ ...EMPTY, channel: channelOptions[0]?.value ?? "" });

  const openEdit = (row: JobRow) =>
    setDraft({
      id: row.id,
      name: row.name,
      kind: row.kind === "daily" ? "daily" : "interval",
      seconds: Math.max(5, Math.round(Number(row.schedule?.seconds ?? 3600))),
      at: row.schedule?.at ?? "09:30",
      channel: row.params?.channel ?? "",
      text: row.params?.text ?? "",
      enabled: row.enabled,
    });

  const save = async () => {
    if (!draft) return;
    if (!draft.name.trim()) {
      message.error("名称不能为空");
      return;
    }
    if (!draft.channel || !draft.text.trim()) {
      message.error("目标和内容都要填");
      return;
    }
    setSaving(true);
    try {
      await post("/api/jobs", {
        id: draft.id,
        name: draft.name.trim(),
        kind: draft.kind,
        schedule: draft.kind === "daily" ? { at: draft.at } : { seconds: draft.seconds },
        action: "send_text",
        params: { channel: draft.channel, text: draft.text },
        enabled: draft.enabled,
      });
      message.success(draft.id ? "已保存" : "已创建");
      setDraft(null);
      jobs.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (row: JobRow, enabled: boolean) => {
    setBusy(row.id);
    try {
      await post(`/api/jobs/${encodeURIComponent(row.id)}/${enabled ? "enable" : "disable"}`);
      jobs.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const runNow = async (row: JobRow) => {
    setBusy(row.id);
    try {
      await post(`/api/jobs/${encodeURIComponent(row.id)}/run`);
      message.success("已执行一次（不改动既定节奏）");
      jobs.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = (row: JobRow) => {
    Modal.confirm({
      title: `删除「${row.name}」？`,
      content: "删掉后不会再自动发送。历史结果一并消失。",
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await del(`/api/jobs/${encodeURIComponent(row.id)}`);
          message.success("已删除");
          jobs.reload();
        } catch (exc) {
          message.error((exc as Error).message);
        }
      },
    });
  };

  return (
    <Flex vertical gap={16}>
      {jobs.error ? <Alert type="error" showIcon message={jobs.error.message} /> : null}

      <Alert
        type="info"
        showIcon
        message="定时任务只能「发一段文本到指定频道」"
        description="不支持定时执行命令 —— 避免面板变成远程执行入口。任务落库，重启不丢；单次失败会记录原因并继续排下一次。"
      />

      <Card
        size="small"
        title={`任务（${rows.length}）`}
        extra={
          <Button size="small" type="primary" onClick={openNew} disabled={channelOptions.length === 0}>
            新建任务
          </Button>
        }
      >
        {channelOptions.length === 0 ? (
          <Alert
            type="warning"
            showIcon
            message="还没有启用中的频道"
            description="先去「白名单与风控」启用一个群，否则任务没有可发的目标。"
          />
        ) : null}
        <Table<JobRow>
          rowKey="id"
          size="small"
          loading={jobs.loading}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 980 }}
          columns={[
            { title: "名称", dataIndex: "name", width: 150, ellipsis: true },
            { title: "调度", width: 130, render: (_v, row) => scheduleText(row) },
            {
              title: "目标",
              width: 210,
              render: (_v, row) => (
                <Typography.Text code style={{ fontSize: 12 }}>
                  {row.params?.channel ?? "—"}
                </Typography.Text>
              ),
            },
            {
              title: "内容",
              ellipsis: true,
              render: (_v, row) => (
                <Typography.Text type="secondary" ellipsis>
                  {row.params?.text ?? ""}
                </Typography.Text>
              ),
            },
            {
              title: "启用",
              width: 80,
              render: (_v, row) => (
                <Switch
                  size="small"
                  checked={row.enabled}
                  loading={busy === row.id}
                  onChange={(checked) => void toggle(row, checked)}
                />
              ),
            },
            {
              title: "上次结果",
              width: 150,
              render: (_v, row) => {
                if (!row.last_status) return <Tag>从未执行</Tag>;
                if (row.last_status === "ok") return <Tag color="green">成功</Tag>;
                if (row.last_status === "pending") return <Tag>待执行</Tag>;
                return (
                  <Typography.Text type="danger" ellipsis style={{ maxWidth: 150 }} title={row.last_error ?? ""}>
                    {row.last_error ?? "失败"}
                  </Typography.Text>
                );
              },
            },
            {
              title: "下次",
              width: 140,
              render: (_v, row) => (
                <Flex vertical>
                  <Typography.Text>{dueText(row.due_in)}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    {fmtTime(row.next_run_at)}
                  </Typography.Text>
                </Flex>
              ),
            },
            {
              title: "操作",
              width: 170,
              render: (_v, row) => (
                <Space size={4}>
                  <Button size="small" type="link" loading={busy === row.id} onClick={() => void runNow(row)}>
                    立即执行
                  </Button>
                  <Button size="small" type="link" onClick={() => openEdit(row)}>
                    编辑
                  </Button>
                  <Button size="small" type="link" danger onClick={() => remove(row)}>
                    删除
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={draft !== null}
        title={draft?.id ? "编辑任务" : "新建任务"}
        onCancel={() => setDraft(null)}
        onOk={() => void save()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
        width={560}
      >
        <Flex vertical gap={12}>
          <div>
            <Typography.Text type="secondary">名称</Typography.Text>
            <Input
              value={draft?.name ?? ""}
              placeholder="例如：早报"
              onChange={(event) => setDraft((prev) => (prev ? { ...prev, name: event.target.value } : prev))}
            />
          </div>

          <Flex gap={12}>
            <div style={{ flex: 1 }}>
              <Typography.Text type="secondary">调度方式</Typography.Text>
              <Select
                style={{ width: "100%" }}
                value={draft?.kind ?? "daily"}
                options={[
                  { value: "daily", label: "每天固定时间" },
                  { value: "interval", label: "固定间隔" },
                ]}
                onChange={(value) =>
                  setDraft((prev) => (prev ? { ...prev, kind: value as Draft["kind"] } : prev))
                }
              />
            </div>
            <div style={{ width: 160 }}>
              <Typography.Text type="secondary">{draft?.kind === "daily" ? "时间" : "间隔（秒）"}</Typography.Text>
              {draft?.kind === "daily" ? (
                <Input
                  value={draft.at}
                  placeholder="09:30"
                  onChange={(event) => setDraft((prev) => (prev ? { ...prev, at: event.target.value } : prev))}
                />
              ) : (
                <InputNumber
                  style={{ width: "100%" }}
                  min={5}
                  value={draft?.seconds ?? 3600}
                  onChange={(value) =>
                    setDraft((prev) => (prev ? { ...prev, seconds: Number(value ?? 3600) } : prev))
                  }
                />
              )}
            </div>
          </Flex>

          <div>
            <Typography.Text type="secondary">发到哪个频道</Typography.Text>
            <Select
              style={{ width: "100%" }}
              value={draft?.channel || undefined}
              placeholder="选一个已启用的频道"
              options={channelOptions}
              onChange={(value) => setDraft((prev) => (prev ? { ...prev, channel: value } : prev))}
            />
          </div>

          <div>
            <Typography.Text type="secondary">内容（纯文本，最多 2000 字）</Typography.Text>
            <Input.TextArea
              rows={3}
              value={draft?.text ?? ""}
              placeholder="早上好，今天也要开心"
              onChange={(event) => setDraft((prev) => (prev ? { ...prev, text: event.target.value } : prev))}
            />
          </div>

          <Flex align="center" gap={8}>
            <Switch
              size="small"
              checked={draft?.enabled ?? true}
              onChange={(checked) => setDraft((prev) => (prev ? { ...prev, enabled: checked } : prev))}
            />
            <Typography.Text type="secondary">保存后立即启用</Typography.Text>
          </Flex>
        </Flex>
      </Modal>
    </Flex>
  );
}
