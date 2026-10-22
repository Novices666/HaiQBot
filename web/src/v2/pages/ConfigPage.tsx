/** 配置中心：分组表单热改、vault 凭据状态、原始 TOML、快照回滚。密钥真值永不经过这里。 */
import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Flex,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { post } from "../../api";
import { fmtTime, text, usePoll } from "../lib";
import type { ConfigField, ConfigPayload, HistoryRow } from "../types";

function Control({ field }: { field: ConfigField }) {
  if (field.readonly) return <Input disabled value={text(field.value, "")} />;
  if (field.choices.length > 0) {
    return (
      <Select
        options={field.choices.map((c) => ({ value: c, label: c }))}
        allowClear
        placeholder={text(field.value)}
      />
    );
  }
  if (field.kind === "bool") return <Switch />;
  if (field.kind === "int" || field.kind === "float") {
    return (
      <InputNumber
        style={{ width: "100%" }}
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
      />
    );
  }
  return <Input placeholder={field.kind === "ref" ? "vault 引用名，不是密钥本身" : ""} />;
}

export default function ConfigPage() {
  const cfg = usePoll<ConfigPayload>("/api/config");
  const history = usePoll<HistoryRow[]>("/api/config/history");
  const [saving, setSaving] = useState(false);

  const groups = useMemo(() => {
    const map = new Map<string, ConfigField[]>();
    for (const field of cfg.data?.fields ?? []) {
      const key = field.group_label || field.group || "其它";
      map.set(key, [...(map.get(key) ?? []), field]);
    }
    return [...map.entries()];
  }, [cfg.data]);

  const initial = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const field of cfg.data?.fields ?? []) out[field.path] = field.value;
    return out;
  }, [cfg.data]);

  const save = async (values: Record<string, unknown>) => {
    const changes: Record<string, unknown> = {};
    for (const [path, value] of Object.entries(values)) {
      if (value === undefined) continue;
      if (JSON.stringify(value) !== JSON.stringify(initial[path])) changes[path] = value;
    }
    if (Object.keys(changes).length === 0) {
      message.info("没有改动");
      return;
    }
    setSaving(true);
    try {
      const result = await post<{ changed: string[]; warnings?: string[] }>("/api/config", changes);
      message.success(`已写入 ${result.changed.length} 项`);
      for (const warning of result.warnings ?? []) message.warning(warning);
      cfg.reload();
      history.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const rollback = async (id: string) => {
    try {
      const result = await post<{ restored: string; note: string }>("/api/config/rollback", { id });
      message.success(result.note);
      cfg.reload();
      history.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    }
  };

  return (
    <Flex vertical gap={16}>
      {cfg.error ? <Alert type="error" showIcon message={cfg.error.message} /> : null}
      <Alert
        type="info"
        showIcon
        message="密钥只以 vault 引用的形式出现"
        description="面板只能看到 *_ref 键名与「是否已设置」。要改真值请用 haiq secrets set，写进 vault 后立即生效。"
      />

      <Card size="small" title={cfg.data?.path ?? "配置文件"}>
        <Tabs
          size="small"
          items={[
            {
              key: "form",
              label: "字段",
              children: (
                <Form layout="vertical" size="small" initialValues={initial} onFinish={save}>
                  {groups.map(([label, fields]) => (
                    <Card key={label} size="small" type="inner" title={label} style={{ marginBottom: 12 }}>
                      <Row gutter={12}>
                        {fields.map((field) => (
                          <Col span={8} key={field.path}>
                            <Form.Item
                              name={field.path}
                              label={
                                <Space size={4}>
                                  <span>{field.label}</span>
                                  <Tooltip title={`${field.path} · 生效方式：${field.apply_label}`}>
                                    <Tag color={field.readonly ? "default" : "blue"} style={{ marginInlineStart: 0 }}>
                                      {field.apply_label}
                                    </Tag>
                                  </Tooltip>
                                </Space>
                              }
                              extra={field.help || undefined}
                              valuePropName={field.kind === "bool" ? "checked" : "value"}
                            >
                              <Control field={field} />
                            </Form.Item>
                          </Col>
                        ))}
                      </Row>
                    </Card>
                  ))}
                  <Button type="primary" htmlType="submit" loading={saving}>
                    保存改动
                  </Button>
                </Form>
              ),
            },
            {
              key: "secrets",
              label: `凭据（${cfg.data?.secrets.length ?? 0}）`,
              children: (
                <Table
                  size="small"
                  rowKey={(row: Record<string, unknown>) => String(row.path)}
                  pagination={false}
                  dataSource={cfg.data?.secrets ?? []}
                  columns={[
                    { title: "配置项", dataIndex: "path", width: 260 },
                    { title: "说明", dataIndex: "label", width: 220 },
                    {
                      title: "vault 引用",
                      dataIndex: "ref",
                      render: (v: string) => (
                        <Typography.Text code copyable={{ text: v || "" }}>
                          {v || "—"}
                        </Typography.Text>
                      ),
                    },
                    {
                      title: "状态",
                      dataIndex: "set",
                      width: 110,
                      render: (v: boolean) =>
                        v ? <Tag color="green">已设置</Tag> : <Tag color="red">未设置</Tag>,
                    },
                  ]}
                />
              ),
            },
            {
              key: "raw",
              label: "原始 TOML",
              children: (
                <pre
                  style={{
                    maxHeight: 520,
                    overflow: "auto",
                    fontSize: 12,
                    lineHeight: 1.6,
                    fontFamily: "JetBrains Mono, Consolas, monospace",
                    margin: 0,
                  }}
                >
                  {cfg.data?.raw ?? ""}
                </pre>
              ),
            },
            {
              key: "history",
              label: `快照（${history.data?.length ?? 0}）`,
              children: (
                <Table<HistoryRow>
                  size="small"
                  rowKey="id"
                  pagination={{ pageSize: 10, hideOnSinglePage: true }}
                  dataSource={history.data ?? []}
                  locale={{ emptyText: "写入前会自动快照，改了才有" }}
                  columns={[
                    { title: "快照", dataIndex: "id" },
                    { title: "时间", dataIndex: "ts", width: 180, render: (v: number) => fmtTime(v) },
                    { title: "大小", dataIndex: "size", width: 100, render: (v: number) => `${v} B` },
                    {
                      title: "操作",
                      width: 120,
                      render: (_v, row) => (
                        <Button size="small" danger onClick={() => void rollback(row.id)}>
                          回滚到这份
                        </Button>
                      ),
                    },
                  ]}
                />
              ),
            },
          ]}
        />
      </Card>
    </Flex>
  );
}