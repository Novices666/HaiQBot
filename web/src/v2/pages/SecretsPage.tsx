/** 凭据管理：面板里的 vault（密码管理器式）。
 *
 * 约定：
 * - 列表**不显示值**；点「查看」才单独请求一次明文，30 秒后自动收起；
 * - 每次查看 / 写入 / 删除，后端都会写审计，前端不用自己留痕；
 * - 写入只进 vault，不会回写 haiq.toml —— 配置里永远只有 `*_ref`。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Flex, Input, Modal, Space, Table, Tag, Typography, message } from "antd";
import { del, get, post } from "../../api";
import { usePoll } from "../lib";

type SecretRow = {
  key: string;
  ref: string;
  plugin: string | null;
  label: string;
  required: boolean;
  set: boolean;
};

type VaultInfo = { path: string; enc: string; backend: string };
type SecretsPayload = { items: SecretRow[]; vault: VaultInfo };

const REVEAL_MS = 30_000;

export default function SecretsPage() {
  const secrets = usePoll<SecretsPayload>("/api/secrets");
  const rows = useMemo(() => secrets.data?.items ?? [], [secrets.data]);
  const vault = secrets.data?.vault;

  const [shown, setShown] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const timers = useRef<Record<string, number>>({});

  const hide = (key: string) => {
    setShown((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    const timer = timers.current[key];
    if (timer) {
      window.clearTimeout(timer);
      delete timers.current[key];
    }
  };

  useEffect(
    () => () => {
      Object.values(timers.current).forEach((timer) => window.clearTimeout(timer));
    },
    [],
  );

  const reveal = async (row: SecretRow) => {
    if (shown[row.key]) {
      hide(row.key);
      return;
    }
    setBusy(row.key);
    try {
      const data = await get<{ value: string | null }>(`/api/secrets/${encodeURIComponent(row.key)}`);
      if (data.value) {
        setShown((prev) => ({ ...prev, [row.key]: data.value as string }));
        timers.current[row.key] = window.setTimeout(() => hide(row.key), REVEAL_MS);
      } else {
        message.info("这条还没有值");
      }
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setBusy(null);
    }
  };

  // ---- 编辑 ----
  const [editing, setEditing] = useState<{ key: string; value: string; isNew: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
  const [suggest, setSuggest] = useState("");

  const save = async () => {
    if (!editing) return;
    if (!editing.key.trim()) {
      message.error("键名不能为空");
      return;
    }
    if (!editing.value.trim()) {
      message.error("值不能为空");
      return;
    }
    setSaving(true);
    try {
      await post("/api/secrets", { key: editing.key.trim(), value: editing.value });
      message.success(editing.isNew ? "已写入" : "已更新");
      setEditing(null);
      setSuggest("");
      hide(editing.key);
      secrets.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const remove = (row: SecretRow) => {
    Modal.confirm({
      title: `删除 ${row.key}？`,
      content: `物理键名 ${row.ref}。删掉后用到它的插件会立刻失效。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await del(`/api/secrets/${encodeURIComponent(row.key)}`);
          message.success("已删除");
          hide(row.key);
          secrets.reload();
        } catch (exc) {
          message.error((exc as Error).message);
        }
      },
    });
  };

  // ---- 轮换主口令 ----
  const [rotating, setRotating] = useState(false);
  const [oldPass, setOldPass] = useState("");
  const [newPass, setNewPass] = useState("");
  const [againPass, setAgainPass] = useState("");
  const [rotatingBusy, setRotatingBusy] = useState(false);

  const doRotate = async () => {
    if (newPass.length < 12) {
      message.error("新口令至少 12 位");
      return;
    }
    if (newPass !== againPass) {
      message.error("两次输入不一致");
      return;
    }
    setRotatingBusy(true);
    try {
      const data = await post<{ entries: number; restart_required: boolean; hint: string }>(
        "/api/secrets/passwd",
        { old: oldPass, new: newPass },
      );
      message.success(`已重写 ${data.entries} 条凭据`);
      Modal.success({
        title: "口令已换，但还没生效",
        content: data.hint,
      });
      setRotating(false);
      setOldPass("");
      setNewPass("");
      setAgainPass("");
      secrets.reload();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setRotatingBusy(false);
    }
  };

  return (
    <Flex vertical gap={16}>
      {secrets.error ? <Alert type="error" showIcon message={secrets.error.message} /> : null}

      <Alert
        type="info"
        showIcon
        message="这里直接改 vault，不会碰 haiq.toml"
        description="配置里始终只有 *_ref 键名；每次查看明文都会写一条 secret.reveal 审计。"
      />

      <Card
        size="small"
        title="凭据存储"
        extra={
          <Space>
            <Button size="small" onClick={() => setEditing({ key: "", value: "", isNew: true })}>
              新增
            </Button>
            <Button size="small" onClick={() => setRotating(true)} disabled={vault?.enc === "none"}>
              轮换主口令
            </Button>
          </Space>
        }
      >
        <Flex gap={24} wrap="wrap">
          <Typography.Text type="secondary">
            加密：<Typography.Text code>{vault?.enc || "—"}</Typography.Text>
          </Typography.Text>
          <Typography.Text type="secondary">
            后端：<Typography.Text code>{vault?.backend || "—"}</Typography.Text>
          </Typography.Text>
          <Typography.Text type="secondary" ellipsis style={{ maxWidth: 520 }}>
            文件：<Typography.Text code>{vault?.path || "—"}</Typography.Text>
          </Typography.Text>
        </Flex>
        {vault?.enc === "none" ? (
          <Alert
            style={{ marginTop: 12 }}
            type="warning"
            showIcon
            message='当前是明文存储（enc="none"），没有主口令可换'
            description='把 haiq.toml 的 [vault] enc 改成 "aead"，再重新录入一遍凭据。'
          />
        ) : null}
      </Card>

      <Card size="small" title={`凭据列表（${rows.length}）`}>
        <Table<SecretRow>
          rowKey="key"
          size="small"
          loading={secrets.loading}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 860 }}
          columns={[
            {
              title: "键名",
              dataIndex: "key",
              render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
            },
            {
              title: "物理键名",
              dataIndex: "ref",
              render: (value: string) => (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {value}
                </Typography.Text>
              ),
            },
            {
              title: "来源",
              dataIndex: "plugin",
              width: 120,
              render: (plugin: string | null) =>
                plugin ? <Tag color="blue">{plugin}</Tag> : <Tag>裸键</Tag>,
            },
            {
              title: "说明",
              dataIndex: "label",
              ellipsis: true,
            },
            {
              title: "状态",
              dataIndex: "set",
              width: 110,
              render: (set: boolean, row) =>
                set ? (
                  <Tag color="green">已设置</Tag>
                ) : row.required ? (
                  <Tag color="red">缺（必需）</Tag>
                ) : (
                  <Tag>未设置</Tag>
                ),
            },
            {
              title: "值",
              width: 260,
              render: (_value: unknown, row) => {
                const value = shown[row.key];
                return value ? (
                  <Space size={4}>
                    <Typography.Text
                      code
                      copyable={{ text: value }}
                      style={{ fontSize: 12, wordBreak: "break-all" }}
                    >
                      {value}
                    </Typography.Text>
                  </Space>
                ) : (
                  <Typography.Text type="secondary">••••••••</Typography.Text>
                );
              },
            },
            {
              title: "操作",
              width: 190,
              render: (_value: unknown, row) => (
                <Space size={4}>
                  <Button
                    size="small"
                    type="link"
                    loading={busy === row.key}
                    disabled={!row.set}
                    onClick={() => void reveal(row)}
                  >
                    {shown[row.key] ? "收起" : "查看"}
                  </Button>
                  <Button
                    size="small"
                    type="link"
                    onClick={() => setEditing({ key: row.key, value: "", isNew: false })}
                  >
                    修改
                  </Button>
                  <Button size="small" type="link" danger disabled={!row.set} onClick={() => remove(row)}>
                    删除
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={editing !== null}
        title={editing?.isNew ? "新增凭据" : `修改 ${editing?.key}`}
        onCancel={() => setEditing(null)}
        onOk={() => void save()}
        confirmLoading={saving}
        okText="写入"
        cancelText="取消"
        destroyOnHidden
      >
        <Flex vertical gap={12}>
          <div>
            <Typography.Text type="secondary">键名（槽位名如 painter.api_key，或 vault 键名）</Typography.Text>
            <Input
              value={editing?.key ?? ""}
              disabled={!editing?.isNew}
              placeholder="painter.api_key 或 HAIQ_XXX"
              onChange={(event) => setEditing((prev) => (prev ? { ...prev, key: event.target.value } : prev))}
            />
          </div>
          <div>
            <Typography.Text type="secondary">值（写入后不再回显，除非点「查看」）</Typography.Text>
            <Input.Password
              value={editing?.value ?? ""}
              placeholder="粘贴密钥"
              autoComplete="new-password"
              onChange={(event) => setEditing((prev) => (prev ? { ...prev, value: event.target.value } : prev))}
            />
          </div>
          {editing?.isNew ? (
            <div>
              <Typography.Text type="secondary">
                没被插件声明的裸键（自定义），直接填名字即可
              </Typography.Text>
              <Space wrap style={{ marginTop: 6 }}>
                {(suggest ? [suggest] : []).map((item) => (
                  <Button key={item} size="small" onClick={() => setEditing((prev) => (prev ? { ...prev, key: item } : prev))}>
                    {item}
                  </Button>
                ))}
              </Space>
            </div>
          ) : null}
        </Flex>
      </Modal>

      <Modal
        open={rotating}
        title="轮换 vault 主口令"
        onCancel={() => setRotating(false)}
        onOk={() => void doRotate()}
        confirmLoading={rotatingBusy}
        okText="换口令"
        cancelText="取消"
        destroyOnHidden
      >
        <Flex vertical gap={12}>
          <Alert
            type="warning"
            showIcon
            message="换完之后要改环境变量并重启"
            description="当前进程仍用旧口令派生密钥，不重启的话后续读取会失败。"
          />
          <Input.Password
            placeholder="当前口令"
            value={oldPass}
            autoComplete="current-password"
            onChange={(event) => setOldPass(event.target.value)}
          />
          <Input.Password
            placeholder="新口令（至少 12 位）"
            value={newPass}
            autoComplete="new-password"
            onChange={(event) => setNewPass(event.target.value)}
          />
          <Input.Password
            placeholder="再输一次新口令"
            value={againPass}
            autoComplete="new-password"
            onChange={(event) => setAgainPass(event.target.value)}
          />
          <Input
            placeholder="可选：自定义键名（如 NAPCAT_WEBUI_TOKEN）"
            value={suggest}
            onChange={(event) => setSuggest(event.target.value)}
          />
        </Flex>
      </Modal>
    </Flex>
  );
}
