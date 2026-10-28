/** 白名单与风控：频道开关的唯一日常入口（CLI 是应急兜底）。 */
import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Flex,
  Form,
  Input,
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
              <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                {verify.data.problems.map((problem, index) => {
                  const row = (channels.data ?? []).find((item) => item.channel === problem.channel);
                  return (
                    <li key={`${problem.channel}-${index}`}>
                      <Typography.Text code>{problem.channel}</Typography.Text> {problem.message}
                      {row ? (
                        <Button size="small" type="link" onClick={() => open(row)}>
                          去修
                        </Button>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
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
          <Space>
            <Typography.Text type="secondary">显示已停用</Typography.Text>
            <Switch size="small" checked={showDisabled} onChange={setShowDisabled} />
            <Button size="small" onClick={() => channels.reload()}>
              刷新
            </Button>
          </Space>
        }
      >
        <Table<ChannelRow>
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
              width: 150,
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
          <Button size="small" onClick={() => candidates.reload()}>
            刷新候选
          </Button>
        }
      >
        <Form
          form={pickForm}
          layout="inline"
          onFinish={(values: { prefix: string; id: string }) => {
            const channel = `${values.prefix}${values.id}`;
            setEditing({ channel, enabled: true, plugins: [], note: "" });
          }}
        >
          <Form.Item name="prefix" initialValue="onebot:group:" rules={[{ required: true }]}>
            <Select
              style={{ width: 190 }}
              options={[
                { value: "onebot:group:", label: "OneBot 群" },
                { value: "onebot:private:", label: "OneBot 私聊" },
                { value: "official:group:", label: "官方 群" },
                { value: "official:private:", label: "官方 私聊" },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="id"
            rules={[{ required: true, message: "从下拉里选，或直接填群号 / QQ 号" }]}
            extra={<CandidateHint errors={candidates.data?.errors ?? []} total={candidates.data?.items?.length ?? 0} />}
          >
            <ChannelPicker candidates={candidates.data?.items ?? []} prefix={prefix} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit">
              下一步：选功能
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Drawer
        open={editing !== null}
        onClose={() => setEditing(null)}
        width={460}
        title={editing ? `开关：${editing.channel}` : ""}
        extra={
          <Button type="primary" loading={saving} onClick={() => void save()}>
            保存
          </Button>
        }
      >
        {editing ? (
          <Flex vertical gap={16}>
            <Flex align="center" gap={8}>
              <Typography.Text>频道启用</Typography.Text>
              <Switch
                checked={editing.enabled}
                onChange={(v) => setEditing({ ...editing, enabled: v })}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                关闭后即使下面的功能全选也不响应
              </Typography.Text>
            </Flex>
            <div>
              <Typography.Text>已开启项</Typography.Text>
              <Select
                mode="tags"
                style={{ width: "100%", marginTop: 8 }}
                value={editing.plugins}
                onChange={(v: string[]) => setEditing({ ...editing, plugins: v })}
                options={options}
                placeholder="选插件 = 全功能；或手填 painter:draw、chat:ask"
                tokenSeparators={[",", " "]}
              />
            </div>
            <div>
              <Typography.Text>备注</Typography.Text>
              <Input.TextArea
                style={{ marginTop: 8 }}
                rows={2}
                value={editing.note}
                onChange={(e) => setEditing({ ...editing, note: e.target.value })}
                placeholder="比如：这是测试群 / 只给管理用"
              />
            </div>
          </Flex>
        ) : null}
      </Drawer>
    </Flex>
  );
}