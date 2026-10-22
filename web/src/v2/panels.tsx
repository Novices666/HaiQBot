/**
 * 插件自定义页渲染器。
 *
 * 只渲染自己认识的 section 类型；不认识的**降级成只读 JSON** —— 插件写错了最多难看，
 * 不会白屏、也不会执行陌生代码。契约见 src/haiqbot/webui/api/panels.py。
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Flex,
  Popconfirm,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { ApiError, get, post } from "../api";

export type PanelInfo = { pid: string; title: string; order: number };
export type PanelField = { label: string; value?: unknown };
export type PanelColumn = { key: string; title: string };
export type PanelSection = { type: string; [key: string]: unknown };
export type PanelPage = { pid: string; title: string; sections: PanelSection[]; actions: string[] };

function str(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** 导航用的插件页清单（失败就当没有，不影响主面板）。 */
export function usePanels(): PanelInfo[] {
  const [items, setItems] = useState<PanelInfo[]>([]);
  useEffect(() => {
    let alive = true;
    get<PanelInfo[]>("/api/panels")
      .then((rows) => {
        if (alive) setItems(rows ?? []);
      })
      .catch(() => {
        /* 插件页是附加能力，拿不到就静默 */
      });
    return () => {
      alive = false;
    };
  }, []);
  return items;
}

function ActionButton({ pid, section, onDone }: { pid: string; section: PanelSection; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const action = String(section.action ?? "");
  const label = String(section.label ?? action);

  const run = async () => {
    setBusy(true);
    try {
      const result = await post<{ message?: string }>(`/api/panels/${pid}/action`, { action });
      message.success(result?.message || `${label} 完成`);
      onDone();
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const button = (
    <Button size="small" type="primary" loading={busy} onClick={() => void run()}>
      {label}
    </Button>
  );
  const confirm = section.confirm ? String(section.confirm) : "";
  if (!confirm) return button;
  return (
    <Popconfirm title={confirm} onConfirm={() => void run()}>
      {button}
    </Popconfirm>
  );
}

function Section({ pid, section, onDone }: { pid: string; section: PanelSection; onDone: () => void }) {
  switch (section.type) {
    case "text":
      return <Typography.Paragraph type="secondary">{str(section.text)}</Typography.Paragraph>;
    case "field_group":
      return (
        <Card size="small" type="inner" title={str(section.title)} style={{ marginBottom: 12 }}>
          <Descriptions size="small" column={2}>
            {((section.fields as PanelField[]) ?? []).map((field) => (
              <Descriptions.Item key={field.label} label={field.label}>
                {str(field.value)}
              </Descriptions.Item>
            ))}
          </Descriptions>
        </Card>
      );
    case "readonly_table": {
      const columns = ((section.columns as PanelColumn[]) ?? []).map((col) => ({
        title: col.title,
        dataIndex: col.key,
        render: (value: unknown) => str(value),
      }));
      return (
        <Card size="small" type="inner" title={str(section.title)} style={{ marginBottom: 12 }}>
          <Table
            size="small"
            rowKey={(_row, index) => String(index)}
            pagination={false}
            dataSource={(section.rows as Record<string, unknown>[]) ?? []}
            columns={columns}
            locale={{ emptyText: "没有数据" }}
          />
        </Card>
      );
    }
    case "action_button":
      return (
        <div style={{ marginBottom: 12 }}>
          <ActionButton pid={pid} section={section} onDone={onDone} />
        </div>
      );
    default:
      return (
        <Card size="small" type="inner" style={{ marginBottom: 12 }}>
          <Tag>未知区块</Tag>
          <Typography.Text type="secondary">
            {" "}
            前端还不认识 <Typography.Text code>{section.type}</Typography.Text>，仅展示原始数据
          </Typography.Text>
          <pre style={{ fontSize: 12, marginBottom: 0, overflowX: "auto" }}>
            {JSON.stringify(section, null, 2)}
          </pre>
        </Card>
      );
  }
}

export default function PanelRenderer({ pid }: { pid: string }) {
  const [page, setPage] = useState<PanelPage | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPage(await get<PanelPage>(`/api/panels/${pid}`));
      setError(null);
    } catch (exc) {
      setError(exc as ApiError);
    } finally {
      setLoading(false);
    }
  }, [pid]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !page) {
    return (
      <Flex justify="center" style={{ padding: 40 }}>
        <Spin />
      </Flex>
    );
  }
  if (error) {
    return <Alert type="error" showIcon message={error.message} description={error.hint} />;
  }
  if (!page) return <Empty description="插件没有返回页面" />;

  return (
    <Flex vertical>
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        {page.title}
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        由插件 <Typography.Text code>{page.pid}</Typography.Text> 声明；内核只负责渲染，不认识其中任何字段。
      </Typography.Paragraph>
      {page.sections.length === 0 ? (
        <Empty description="插件没有给出任何区块" />
      ) : (
        page.sections.map((section, index) => (
          <Section key={`${section.type}-${index}`} pid={page.pid} section={section} onDone={() => void load()} />
        ))
      )}
    </Flex>
  );
}
