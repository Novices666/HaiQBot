/** 白名单与风控：频道开关的唯一日常入口（CLI 是应急兜底）。 */
import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Drawer,
  Flex,
  Form,
  Input,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { del, post } from "../../api";
import { fmtTime, text, usePoll } from "../lib";
import ChannelPicker, { CandidateHint } from "../components/ChannelPicker";
import type { ChannelCandidates, ChannelRow, PluginRow } from "../types";

type Editable = { channel: string; enabled: boolean; plugins: string[]; note: string };
type VerifyProblem = { channel: string; level: string; code: string; message: string };
type VerifyPayload = { problems: VerifyProblem[]; summary: Record<string, number> };

export default function ChannelsPage() {
  const channels = usePoll<ChannelRow[]>("/api/channels");
  const plugins = usePoll<PluginRow[]>("/api/plugins");
  const verify = usePoll<VerifyPayload>("/api/channels/verify", 20000);
  // 候选：协议端真实的群/好友 + 已配置 + 最近出现过（每 60s 刷一次够用）
  const candidates = usePoll<ChannelCandidates>("/api/channels/candidates", 60000);
  const [pickForm] = Form.useForm();
  // 选了「OneBot 群」就只显示群候选，避免把 QQ 号填进群号里
  const prefix = (Form.useWatch("prefix", pickForm) as string | undefined) ?? "onebot:group:";
  const [showDisabled, setShowDisabled] = useState(false);
  const [editing, setEditing] = useState<Editable | null>(null);
  const [saving, setSaving] = useState(false);

  const rows = (channels.data ?? []).filter((r) => showDisabled || Boolean(r.enabled));

  const open = (row: ChannelRow) =>
    setEditing({
      channel: row.channel,
      enabled: Boolean(row.enabled),
      plugins: row.plugins ?? [],
      note: row.note ?? "",
    });

  const save = async () => {
    if (!editing) return;
    setSaving(true);
    try {
      await post(`/api/channels/${encodeURIComponent(editing.channel)}`, {
        enabled: editing.enabled,
        plugins: editing.plugins,
        note: editing.note,
      });
      message.success("已保存，立即生效（gate 已失效重建）");
      setEditing(null);
      channels.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (row: ChannelRow) => {
    try {
      await del(`/api/channels/${encodeURIComponent(row.channel)}`);
      message.success(`已删除 ${row.channel}，该频道回到「什么都不做」`);
      channels.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    }
  };

  // 选项来源：插件列表 + 允许自由填 plugin:feature
  const options = (plugins.data ?? []).map((p) => ({
    value: p.id,
    label: `${p.name || p.id}（全部功能）`,
  }));

  return (
    <Flex vertical gap={16}>
      {verify.data ? (
        <Alert
          type={
            verify.data.summary.error > 0
              ? "error"
              : verify.data.summary.warn > 0
                ? "warning"
                : "success"
          }
          showIcon
          message={
            verify.data.summary.error > 0
              ? `体检：${verify.data.summary.error} 项会导致「配了却不生效」`
              : verify.data.summary.warn > 0
                ? `体检：${verify.data.summary.warn} 项可能白配了（不致命）`
                : "体检通过：没有发现静默失效"
          }
          description={
            verify.data.problems.length === 0 ? undefined : (
              <Space direction="vertical" size={4}>
                {verify.data.problems.map((problem, index) => {
                  const row = (channels.data ?? []).find((item) => item.channel === problem.channel);
                  return (
                    <Flex key={`${problem.channel}-${index}`} align="center" gap={4} wrap>
                      <Typography.Text code>{problem.channel}</Typography.Text>
                      <span>{problem.message}</span>
                      {row ? (
                        <Button size="small" type="link" onClick={() => open(row)}>
                          去修
                        </Button>
                      ) : null}
                    </Flex>
                  );
                })}
              </Space>
            )
          }
        />
      ) : null}

      <Alert
        type="info"
        showIcon
        message="默认全关：数据库里没有记录的频道一律不响应"
        description="这里的 plugins 就是「已开启项」。写插件 id 表示该插件全部功能；写 plugin:feature 只开启某个功能（如 painter:draw、chat:ask）。"
      />

      <Card
        size="small"
        title="频道开关"
        extra={
          <Flex align="center" gap={8} wrap justify="flex-end">
            <Typography.Text type="secondary">显示已停用</Typography.Text>
            <Switch
              checked={showDisabled}
              onChange={setShowDisabled}
              aria-label="显示已停用的频道"
            />
            <Button onClick={() => channels.reload()}>刷新</Button>
          </Flex>
        }
      >
        <Table<ChannelRow>
          scroll={{ x: "max-content" }}
          size="small"
          rowKey="channel"
          loading={!channels.data && !channels.error}
          dataSource={rows}
          pagination={false}
          locale={{ emptyText: "还没有任何频道被开启 —— 当前 bot 对所有群/私聊静默" }}
          columns={[
            {
              title: "频道",
              dataIndex: "channel",
              width: 250,
              render: (v: string) => (
                <Typography.Text code style={{ whiteSpace: "nowrap" }}>
                  {v}
                </Typography.Text>
              ),
            },
            {
              title: "开关",
              dataIndex: "enabled",
              width: 90,
              render: (v: number | boolean) =>
                v ? <Tag color="green">已开启</Tag> : <Tag>已停用</Tag>,
            },
            {
              title: "已开启项",
              dataIndex: "plugins",
              render: (v: string[]) =>
                v?.length ? (
                  <Space size={4} wrap>
                    {v.map((item) => (
                      <Tag key={item} color={item.includes(":") ? "blue" : "default"}>
                        {item}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  <Typography.Text type="secondary">空（等于没开）</Typography.Text>
                ),
            },
            { title: "备注", dataIndex: "note", width: 200, render: (v: string) => text(v, "") },
            {
              title: "更新时间",
              dataIndex: "updated_at",
              width: 172,
              render: (v: number) => fmtTime(v),
            },
            {
              title: "操作",
              width: 160,
              render: (_v, row) => (
                <Space size={4}>
                  <Button size="small" onClick={() => open(row)}>
                    编辑
                  </Button>
                  <Button size="small" danger onClick={() => void remove(row)}>
                    删除
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Card
        size="small"
        title="新建 / 开启一个频道"
        extra={
          <Button onClick={() => candidates.reload()}>刷新候选</Button>
        }
      >
        {/* 竖排 + 栅格：原来的 inline 表单里，候选错误提示会把 Form.Item 撑宽，
            整行顶出视口 180px（实测 879 视口下文档宽 1059）。 */}
        <Form
          form={pickForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values: { prefix: string; id: string }) => {
            const channel = `${values.prefix}${values.id}`;
            setEditing({ channel, enabled: true, plugins: [], note: "" });
          }}
        >
          <Row gutter={[12, 0]}>
            <Col xs={24} md={8} lg={7}>
              <Form.Item name="prefix" label="频道类型" initialValue="onebot:group:" rules={[{ required: true }]}>
                <Select
                  options={[
                    { value: "onebot:group:", label: "OneBot 群" },
                    { value: "onebot:private:", label: "OneBot 私聊" },
                    { value: "official:group:", label: "官方 群" },
                    { value: "official:private:", label: "官方 私聊" },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col xs={24} md={10} lg={11}>
              <Form.Item
                name="id"
                label="群号 / QQ 号"
                rules={[{ required: true, message: "从下拉里选，或直接填群号 / QQ 号" }]}
                extra={<CandidateHint errors={candidates.data?.errors ?? []} total={candidates.data?.items?.length ?? 0} />}
              >
                <ChannelPicker candidates={candidates.data?.items ?? []} prefix={prefix} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6} lg={6}>
              <Form.Item label=" " colon={false}>
                <Button type="primary" htmlType="submit" block>
                  下一步：选功能
                </Button>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Card>

      <Drawer
        open={editing !== null}
        onClose={() => setEditing(null)}
        width={460}
        destroyOnHidden
        title={editing ? `开关：${editing.channel}` : ""}
        extra={
          <Button type="primary" loading={saving} onClick={() => void save()}>
            保存
          </Button>
        }
      >
        {editing ? (
          <Form layout="vertical" component={false}>
            <Form.Item label="频道启用" htmlFor="ch-enabled" extra="关闭后即使下面的功能全选也不响应">
              <Switch
                id="ch-enabled"
                checked={editing.enabled}
                onChange={(v) => setEditing({ ...editing, enabled: v })}
              />
            </Form.Item>
            <Form.Item
              label="已开启项"
              htmlFor="ch-plugins"
              extra="写插件 id = 该插件全部功能；写 plugin:feature 只开某一项，例如 painter:draw、chat:ask"
            >
              <Select
                id="ch-plugins"
                mode="tags"
                style={{ width: "100%" }}
                value={editing.plugins}
                onChange={(v: string[]) => setEditing({ ...editing, plugins: v })}
                options={options}
                placeholder="选插件，或直接输入 plugin:feature"
                tokenSeparators={[",", " "]}
              />
            </Form.Item>
            <Form.Item label="备注" htmlFor="ch-note">
              <Input.TextArea
                id="ch-note"
                rows={2}
                value={editing.note}
                onChange={(e) => setEditing({ ...editing, note: e.target.value })}
                placeholder="比如：这是测试群 / 只给管理用"
              />
            </Form.Item>
          </Form>
        ) : null}
      </Drawer>
    </Flex>
  );
}
