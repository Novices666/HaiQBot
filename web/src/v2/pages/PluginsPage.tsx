/** 插件与能力：装载状态、命令、按 config_schema 渲染配置表单、凭据是否已设置。 */
import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Flex,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { post } from "../../api";
import { text, usePoll } from "../lib";
import LiveField from "../components/LiveField";
import type {
  CommandRow,
  PluginRow,
  SchemaField,
  SecretSlot,
  ServiceInfo,
} from "../types";

const ROLE_NAME: Record<number, string> = { 1: "群友", 2: "管理员", 3: "群主" };

function SchemaForm({ plugin, onSaved }: { plugin: PluginRow; onSaved: () => void }) {
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  // 稳定引用：LiveField 靠它取「还没保存」的值（改了 base_url 不必先保存才能拉列表）
  const collect = useCallback(() => form.getFieldsValue() as Record<string, unknown>, [form]);
  const schema = plugin.config_schema ?? [];
  const initial: Record<string, unknown> = {};
  for (const field of schema) {
    const current = plugin.config?.[field.key];
    initial[field.key] = current !== undefined ? current : field.default;
  }

  const save = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      await post(`/api/plugins/${plugin.id}/config`, values);
      message.success(`${plugin.id} 配置已保存`);
      onSaved();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (schema.length === 0) {
    return <Typography.Text type="secondary">该插件没有声明可配置项（自由配置请在配置中心改原始 TOML）。</Typography.Text>;
  }

  return (
    <Form form={form} layout="vertical" initialValues={initial} onFinish={save} size="small">
      <Flex wrap gap={16}>
        {schema.map((field: SchemaField) => (
          <Form.Item
            key={field.key}
            name={field.key}
            label={field.label ?? field.key}
            extra={field.description}
            style={{ minWidth: 260, flex: "1 1 260px", marginBottom: 12 }}
          >
            {field.dynamic ? (
              <LiveField pluginId={plugin.id} field={field} collect={collect} />
            ) : field.choices && field.choices.length > 0 ? (
              <Select
                options={field.choices.map((c) => ({ value: c, label: c }))}
                allowClear
              />
            ) : field.kind === "bool" ? (
              <Switch />
            ) : field.kind === "int" || field.kind === "float" ? (
              <InputNumber style={{ width: "100%" }} />
            ) : (
              <Input />
            )}
          </Form.Item>
        ))}
      </Flex>
      <Button type="primary" htmlType="submit" loading={saving}>
        保存插件配置
      </Button>
    </Form>
  );
}

function SecretsView({ slots }: { slots: SecretSlot[] }) {
  if (slots.length === 0) {
    return <Typography.Text type="secondary">该插件不需要凭据。</Typography.Text>;
  }
  return (
    <Table<SecretSlot>
      size="small"
      rowKey="slot"
      pagination={false}
      dataSource={slots}
      columns={[
        { title: "槽位", dataIndex: "slot", width: 160 },
        { title: "说明", dataIndex: "label" },
        {
          title: "vault 引用",
          dataIndex: "ref",
          render: (v: string) => (
            <Typography.Text code copyable={{ text: v }}>
              {v || "—"}
            </Typography.Text>
          ),
        },
        {
          title: "状态",
          dataIndex: "set",
          width: 110,
          render: (v: boolean, row) =>
            v ? <Tag color="green">已设置</Tag> : <Tag color={row.required ? "red" : "default"}>未设置</Tag>,
        },
      ]}
    />
  );
}

export default function PluginsPage() {
  const { data, error, reload } = usePoll<PluginRow[]>("/api/plugins");
  const [busy, setBusy] = useState<string | null>(null);

  const toggle = async (row: PluginRow) => {
    setBusy(row.id);
    try {
      // 后端会说清楚「有没有写进配置文件」：写不进去（只读属性 / 被独占 /
      // 容器只读挂载）时不能还报「重启后依然生效」——那是假话。
      const result = await post<{ persisted?: boolean; warning?: string }>(
        `/api/plugins/${row.id}/${row.enabled ? "disable" : "enable"}`,
      );
      const verb = row.enabled ? "停用" : "启用";
      if (result?.persisted === false) {
        message.warning(`${row.id} 已${verb}（仅本进程生效）`);
        if (result.warning) message.warning({ content: result.warning, duration: 10 });
      } else {
        message.success(`${row.id} 已${verb}（重启后依然生效）`);
        if (result?.warning) message.warning(result.warning);
      }
      reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const reloadOne = async (row: PluginRow) => {
    setBusy(row.id);
    try {
      await post(`/api/plugins/${row.id}/reload`);
      message.success(`${row.id} 已重载`);
      reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Flex vertical gap={16}>
      {error ? <Alert type="error" showIcon message={error.message} /> : null}
      <RuntimePanel />
      <Card size="small" title="插件（点击行展开：命令 / 配置 / 凭据）">
        <Table<PluginRow>
          size="small"
          rowKey="id"
          loading={!data && !error}
          dataSource={data ?? []}
          pagination={false}
          expandable={{
            expandedRowRender: (row) => (
              <Tabs
                size="small"
                items={[
                  {
                    key: "schema",
                    label: "配置",
                    children: <SchemaForm plugin={row} onSaved={reload} />,
                  },
                  {
                    key: "commands",
                    label: `命令（${row.commands?.length ?? 0}）`,
                    children: (
                      <Table
                        size="small"
                        rowKey={(c) => `${c.plugin}:${c.command}`}
                        pagination={false}
                        dataSource={row.commands ?? []}
                        locale={{ emptyText: "没有注册命令（纯消息钩子或未加载）" }}
                        columns={[
                          { title: "命令", dataIndex: "command", width: 140 },
                          {
                            title: "别名",
                            dataIndex: "aliases",
                            width: 180,
                            render: (v: string[]) => (v?.length ? v.join(" / ") : "—"),
                          },
                          { title: "说明", dataIndex: "description" },
                          {
                            title: "权限",
                            dataIndex: "min_role",
                            width: 100,
                            render: (v: number) => ROLE_NAME[v] ?? text(v),
                          },
                        ]}
                      />
                    ),
                  },
                  {
                    key: "secrets",
                    label: `凭据（${row.secrets?.length ?? 0}）`,
                    children: <SecretsView slots={row.secrets ?? []} />,
                  },
                  {
                    key: "meta",
                    label: "元信息",
                    children: (
                      <Descriptions size="small" column={2} bordered>
                        <Descriptions.Item label="ID">{row.id}</Descriptions.Item>
                        <Descriptions.Item label="来源">{text(row.source)}</Descriptions.Item>
                        <Descriptions.Item label="版本">{text(row.version)}</Descriptions.Item>
                        <Descriptions.Item label="能力">
                          {row.capabilities?.length ? row.capabilities.join(", ") : "—"}
                        </Descriptions.Item>
                        <Descriptions.Item label="提供">{text(row.provides)}</Descriptions.Item>
                        <Descriptions.Item label="依赖">
                          {row.requires?.length ? row.requires.join(", ") : "—"}
                        </Descriptions.Item>
                        <Descriptions.Item label="规则" span={2}>
                          {row.rules?.length ? row.rules.join(" · ") : "—"}
                        </Descriptions.Item>
                      </Descriptions>
                    ),
                  },
                ]}
              />
            ),
          }}
          columns={[
            {
              title: "插件",
              dataIndex: "id",
              width: 150,
              render: (v: string, row) => (
                <Space direction="vertical" size={0}>
                  <Typography.Text strong>{row.name || v}</Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                    {v} · {text(row.version)}
                  </Typography.Text>
                </Space>
              ),
            },
            { title: "说明", dataIndex: "description", render: (v: unknown) => text(v, "") },
            {
              title: "状态",
              width: 190,
              render: (_v, row) => (
                <Space size={4} wrap>
                  {row.loaded ? <Tag color="green">已加载</Tag> : <Tag>未加载</Tag>}
                  {row.enabled ? <Tag color="blue">已启用</Tag> : <Tag>已停用</Tag>}
                  {row.blocked ? <Tag color="red">被依赖阻塞</Tag> : null}
                </Space>
              ),
            },
            {
              title: "操作",
              width: 190,
              render: (_v, row) => (
                <Space size={4}>
                  <Switch
                    size="small"
                    checked={row.enabled}
                    loading={busy === row.id}
                    onChange={() => void toggle(row)}
                  />
                  <Button size="small" onClick={() => void reloadOne(row)} loading={busy === row.id}>
                    重载
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>
    </Flex>
  );
}


/**
 * 运行时能力与命令。
 *
 * 和下面「插件」那张表的区别：这里是**运行时真实注册**的结果
 * （`/api/services` 来自 `runtime.providers`），而插件表里的 `provides`
 * 只是插件的**声明**。两者对不上就说明「声明了但没注册成功」。
 */
function RuntimePanel() {
  const services = usePoll<Record<string, ServiceInfo>>("/api/services");
  const commands = usePoll<CommandRow[]>("/api/commands");

  const serviceRows = Object.entries(services.data ?? {}).map(([name, info]) => ({
    name,
    owner: info?.owner ?? null,
    spec: info?.spec,
  }));

  return (
    <Card size="small" title="运行时能力与命令">
      <Tabs
        size="small"
        items={[
          {
            key: "services",
            label: `能力提供者（${serviceRows.length}）`,
            children: (
              <Flex vertical gap={8}>
                {services.error ? <Alert type="error" showIcon message={services.error.message} /> : null}
                <Table
                  size="small"
                  rowKey="name"
                  loading={!services.data && !services.error}
                  dataSource={serviceRows}
                  pagination={false}
                  locale={{ emptyText: "还没有插件注册能力" }}
                  columns={[
                    {
                      title: "能力",
                      dataIndex: "name",
                      render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
                    },
                    { title: "提供者", dataIndex: "owner", width: 160, render: (v: unknown) => text(v) },
                    {
                      title: "契约",
                      dataIndex: "spec",
                      render: (v: unknown) => (
                        <Typography.Text style={{ fontFamily: "monospace", fontSize: 12 }}>
                          {text(v, "—")}
                        </Typography.Text>
                      ),
                    },
                  ]}
                />
              </Flex>
            ),
          },
          {
            key: "commands",
            label: `命令速查（${(commands.data ?? []).length}）`,
            children: (
              <Flex vertical gap={8}>
                {commands.error ? <Alert type="error" showIcon message={commands.error.message} /> : null}
                <Table<CommandRow>
                  size="small"
                  rowKey={(c) => `${c.plugin}:${c.command}`}
                  loading={!commands.data && !commands.error}
                  dataSource={commands.data ?? []}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }}
                  locale={{ emptyText: "没有注册任何命令" }}
                  columns={[
                    {
                      title: "命令",
                      dataIndex: "command",
                      width: 150,
                      render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
                    },
                    {
                      title: "别名",
                      dataIndex: "aliases",
                      width: 160,
                      render: (v: string[]) => (v?.length ? v.join(" / ") : "—"),
                    },
                    { title: "用法", dataIndex: "usage", width: 200, render: (v: unknown) => text(v, "—") },
                    { title: "说明", dataIndex: "description", render: (v: unknown) => text(v, "—") },
                    {
                      title: "权限",
                      dataIndex: "min_role",
                      width: 90,
                      render: (v: number) => ROLE_NAME[v] ?? "群友",
                    },
                    { title: "插件", dataIndex: "plugin", width: 110 },
                  ]}
                />
              </Flex>
            ),
          },
        ]}
      />
    </Card>
  );
}
