/** 配置中心：分组表单热改、vault 凭据状态、原始 TOML、快照回滚。密钥真值永不经过这里。 */
import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
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
import { fmtTime, usePoll } from "../lib";
import type { ConfigField, ConfigPayload, HistoryRow } from "../types";

/** 把 `{onebot: {host: x}}` 摊回 `{"onebot.host": x}` —— 表单 name 用了数组，取回来要还原。 */
function flatten(obj: Record<string, unknown>, prefix = ""): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      Object.assign(out, flatten(value as Record<string, unknown>, path));
    } else {
      out[path] = value;
    }
  }
  return out;
}

/** 反向：把 `{"onebot.host": x}` 还原成 `{onebot: {host: x}}` —— 表单 name 是数组，要嵌套形状。 */
function unflatten(flat: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [path, value] of Object.entries(flat)) {
    const parts = path.split(".");
    let node = out;
    for (let i = 0; i < parts.length - 1; i += 1) {
      const key = parts[i];
      if (!node[key] || typeof node[key] !== "object") node[key] = {};
      node = node[key] as Record<string, unknown>;
    }
    node[parts[parts.length - 1]] = value;
  }
  return out;
}

/**
 * 表单控件。
 *
 * ⚠️ 必须把 `Form.Item` 注入的 `value` / `onChange` **透传**给真正的 antd 控件
 * （`...injected`）。之前这里只接 `field`，注入的 props 被丢掉 —— 表现就是
 * 整页输入框永远是空的：后端 50 个字段都带值，表单也 setFieldsValue 了，
 * 但值没有落到 DOM 上。
 */
type ControlProps = { field: ConfigField } & Record<string, unknown>;

/** 字段元信息：路径 / 生效方式 / 值来源 / 帮助，统一放在输入框下面一行小字。
 *  之前全塞在 label 里，一个字段挂 2–3 个 Tag，标签行被挤成两三行。 */
function FieldMeta({ field }: { field: ConfigField }) {
  const badge =
    field.source === "env"
      ? { color: "orange", text: "环境变量" }
      : field.source === "default"
        ? { color: "default", text: "默认值" }
        : null;
  return (
    <Space size={4} wrap style={{ fontSize: 12, lineHeight: "20px" }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {field.path}
      </Typography.Text>
      <Tag color={field.readonly ? "default" : "blue"}>{field.apply_label}</Tag>
      {badge ? (
        <Tooltip
          title={
            field.source === "env"
              ? "这个值来自环境变量覆盖（如 HAIQ_DATA_DIR），文件里写的值不生效"
              : "文件里没写这个键，当前用的是默认值"
          }
        >
          <Tag color={badge.color}>{badge.text}</Tag>
        </Tooltip>
      ) : null}
      {field.help ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {field.help}
        </Typography.Text>
      ) : null}
    </Space>
  );
}

function Control({ field, ...injected }: ControlProps) {
  if (field.readonly) return <Input disabled {...injected} />;
  if (field.choices.length > 0) {
    return (
      <Select options={field.choices.map((c) => ({ value: c, label: c }))} allowClear {...injected} />
    );
  }
  if (field.kind === "bool") return <Switch {...injected} />;
  if (field.kind === "int" || field.kind === "float") {
    return (
      <InputNumber
        style={{ width: "100%" }}
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
        {...injected}
      />
    );
  }
  return <Input placeholder={field.kind === "ref" ? "vault 引用名，不是密钥本身" : ""} {...injected} />;
}

export default function ConfigPage() {
  const cfg = usePoll<ConfigPayload>("/api/config");
  const history = usePoll<HistoryRow[]>("/api/config/history");
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();

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

  const initialNested = useMemo(() => unflatten(initial), [initial]);

  // antd 的 initialValues 只在**首次挂载**时生效，而这里是先挂载、数据后到，
  // 所以必须在拿到 /api/config 之后显式灌一次 —— 否则整页输入框永远是空的。
  // 注意灌的必须是**嵌套形状**（和 name={path.split(".")} 对应），
  // 直接灌 "onebot.host" 这种点号键会因为键名歧义读不到。
  useEffect(() => {
    if (!cfg.data) return;
    form.setFieldsValue(initialNested);
  }, [cfg.data, form, initialNested]);

  const save = async (values: Record<string, unknown>) => {
    // 表单 name 用了数组（见下面 name={field.path.split(".")}），取回来是嵌套对象，
    // 这里摊平成 "onebot.host" 这样的路径再和后端比对/提交。
    const flat = flatten(values);
    const changes: Record<string, unknown> = {};
    for (const [path, value] of Object.entries(flat)) {
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
                <Form form={form} layout="vertical" initialValues={initialNested} onFinish={save}>
                  <Collapse
                    size="small"
                    // groups 一开始是空的（数据后到），defaultActiveKey 只在挂载时读一次，
                    // 所以用 key 让它随分组重建，保证"第一组默认展开"。
                    key={groups.map(([label]) => label).join("|")}
                    defaultActiveKey={groups.length ? [groups[0][0]] : []}
                    items={groups.map(([label, fields]) => ({
                      key: label,
                      label: `${label}（${fields.length} 项）`,
                      children: (
                        <Row gutter={[12, 0]}>
                        {fields.map((field) => (
                          <Col xs={24} sm={12} lg={8} key={field.path}>
                            <Form.Item
                              name={field.path.split(".")}
                              label={field.label}
                              extra={<FieldMeta field={field} />}
                              valuePropName={field.kind === "bool" ? "checked" : "value"}
                            >
                              <Control field={field} />
                            </Form.Item>
                          </Col>
                        ))}
                        </Row>
                      ),
                    }))}
                  />
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
                  scroll={{ x: "max-content" }}
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
                  scroll={{ x: "max-content" }}
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
