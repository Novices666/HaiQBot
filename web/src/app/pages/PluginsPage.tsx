/** 插件与能力：装载状态、命令、按 config_schema 渲染配置表单、凭据是否已设置。 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
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

/** 前缀 → 中文名。只给确定的那几个，其余原样显示前缀，不硬编一套假分类。 */
const PREFIX_LABEL: Record<string, string> = {
  image: "生图",
  chat: "对话",
  moderation: "内容审核",
  moderate: "内容审核",
  router: "路由",
  guard: "风控",
  media: "媒体",
  log: "日志",
  onebot: "OneBot",
  webui: "面板",
};

type SchemaGroup = { key: string; label: string; fields: SchemaField[] };

/**
 * 把 config_schema 分堆。
 *
 * 为什么必须分：assistant 一个插件就有 29 项配置，平铺出来是一堵墙。
 * 分堆规则只依赖插件作者自己起的字段前缀（`chat_*` / `image_*` / `moderation_*`），
 * 也就是「作者本来怎么想的功能划分」，不是我另造一套分类。
 * 组数过多时，只有 1 项的那些零散组并进「其他」，免得出现 8 个各含一项的标题。
 */
function groupSchema(schema: SchemaField[]): SchemaGroup[] {
  if (schema.length === 0) return [];
  const buckets = new Map<string, SchemaField[]>();
  for (const field of schema) {
    const cut = field.key.indexOf("_");
    const prefix = cut > 0 ? field.key.slice(0, cut) : "general";
    buckets.set(prefix, [...(buckets.get(prefix) ?? []), field]);
  }
  const groups: SchemaGroup[] = [];
  for (const [prefix, fields] of buckets) {
    const label = PREFIX_LABEL[prefix] ?? prefix;
    // 不同前缀可能落到同一个中文标签（moderation_* 和 moderate_*），必须并成一组，
    // 否则会出现两个都叫「内容审核」的面板。
    const same = groups.find((group) => group.label === label);
    if (same) same.fields.push(...fields);
    else groups.push({ key: prefix, label, fields: [...fields] });
  }
  if (groups.length <= 4) return groups;
  const kept = groups.filter((group) => group.fields.length > 1);
  const loose = groups.filter((group) => group.fields.length === 1).flatMap((group) => group.fields);
  return loose.length > 0 ? [...kept, { key: "other", label: "其他", fields: loose }] : kept;
}

function SchemaForm({ plugin, onSaved }: { plugin: PluginRow; onSaved: () => void }) {
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm();
  // 稳定引用：LiveField 靠它取「还没保存」的值（改了 base_url 不必先保存才能拉列表）
  const collect = useCallback(() => form.getFieldsValue() as Record<string, unknown>, [form]);
  const schema = plugin.config_schema ?? [];

  const initial = useMemo(() => {
    const values: Record<string, unknown> = {};
    for (const field of schema) {
      const current = plugin.config?.[field.key];
      values[field.key] = current !== undefined ? current : field.default;
    }
    return values;
  }, [schema, plugin.config]);

  // 关键：antd 的 initialValues 只在 Form **首次挂载**时读一次。
  // 插件配置在折叠面板里，收起再展开就是一次重挂载 —— 那时 initialValues 已经用掉了，
  // 字段会全部变空（看起来像「配置读不出来」）。所以每次初始值变化都要显式灌一次。
  useEffect(() => {
    form.setFieldsValue(initial);
  }, [form, initial]);

  const groups = useMemo(() => groupSchema(schema), [schema]);

  const save = async (values: Record<string, unknown>) => {
    // 只提交**真正改动**的字段。
    // 后端 apply_plugin_config 会把收到的每个键都写进插件配置文件，
    // 而面板的表单值是「文件值 + schema 默认值」的合集 —— 全量提交等于
    // 把文件里本来没有、只有默认值的键凭空补写一行（实测点一次保存就多出
    // onebot.port = 3002）。这里按初始值做差异过滤，没改动就不发请求。
    const changes: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(values)) {
      if (value === undefined) continue;
      if (JSON.stringify(value) !== JSON.stringify(initial[key])) changes[key] = value;
    }
    if (Object.keys(changes).length === 0) {
      message.info("没有改动");
      return;
    }
    setSaving(true);
    try {
      await post(`/api/plugins/${plugin.id}/config`, changes);
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

  const fields = (list: SchemaField[]) => (
    <Row gutter={[16, 0]}>
      {list.map((field) => (
        <Col key={field.key} xs={24} md={12} xxl={8}>
          <Form.Item
            name={field.key}
            label={field.label ?? field.key}
            extra={field.description}
            style={{ marginBottom: 14 }}
          >
            {/* 控件必须**直接**放在 Form.Item 里：antd 靠 cloneElement 把 value/onChange
                注入到直接子元素，中间再包一层自定义组件就会全部丢失 ——
                表现是「配置文件里有值，面板上一片空白」。 */}
            {field.dynamic ? (
              <LiveField pluginId={plugin.id} field={field} collect={collect} />
            ) : field.choices && field.choices.length > 0 ? (
              <Select options={field.choices.map((c) => ({ value: c, label: c }))} allowClear />
            ) : field.kind === "bool" ? (
              <Switch />
            ) : field.kind === "int" || field.kind === "float" ? (
              <InputNumber style={{ width: "100%" }} />
            ) : (
              <Input />
            )}
          </Form.Item>
        </Col>
      ))}
    </Row>
  );

  const single = groups.length <= 1;

  return (
    <Form form={form} layout="vertical" initialValues={initial} onFinish={save}>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
        共 {schema.length} 项
        {single ? "" : `，按功能分成 ${groups.length} 组（点标题展开）`}。改完点最下面的保存。
      </Typography.Paragraph>

      {single ? (
        fields(schema)
      ) : (
        <Collapse
          size="small"
          defaultActiveKey={[groups[0].key]}
          items={groups.map((group) => ({
            key: group.key,
            label: `${group.label}（${group.fields.length}）`,
            children: fields(group.fields),
          }))}
        />
      )}

      <Button type="primary" htmlType="submit" loading={saving} style={{ marginTop: 14 }}>
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
      scroll={{ x: "max-content" }}
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
  /** 一次只展开一个插件：否则 5 个插件各展开一份配置，页面瞬间很长。 */
  const [openRow, setOpenRow] = useState<string | null>(null);

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
          scroll={{ x: "max-content" }}
          size="small"
          rowKey="id"
          loading={!data && !error}
          dataSource={data ?? []}
          pagination={false}
          expandable={{
            // 手风琴：展开的配置是表单，同时开多个等于把页面拉成一条长卷
            expandedRowKeys: openRow ? [openRow] : [],
            onExpandedRowsChange: (keys) => {
              const next = (keys as string[]).filter((k) => k !== openRow);
              setOpenRow(next[0] ?? null);
            },
            expandedRowRender: (row) => (
              // 关键：展开行在 max-content 的表格里，宽度会被撑到上万像素，
              // 于是 flex 布局的所有字段全排成一行。这里给死宽度，把它掰回正常栏宽。
              <div style={{ width: "min(920px, calc(100vw - 56px))" }}>
                <Tabs
                  size="small"
                  items={[
                    {
                      key: "schema",
                      label: `配置（${row.config_schema?.length ?? 0}）`,
                      children: <SchemaForm plugin={row} onSaved={reload} />,
                    },
                    {
                      key: "commands",
                      label: `命令（${row.commands?.length ?? 0}）`,
                      children: (
                        <Table
                          size="small"
                          scroll={{ x: "max-content" }}
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
                        <Descriptions size="small" column={{ xs: 1, sm: 1, md: 2 }} bordered>
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
              </div>
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
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
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
                  scroll={{ x: "max-content" }}
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
                  scroll={{ x: "max-content" }}
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
