/** 命令工作台：只预演解析与匹配，不触发插件、不发消息。 */
import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Flex,
  Form,
  Input,
  Row,
  Select,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { post } from "../../api";
import type { MatchedRule, ParseResult, SkippedRule } from "../types";

export default function WorkbenchPage() {
  const [result, setResult] = useState<ParseResult | null>(null);
  const [running, setRunning] = useState(false);

  const run = async (values: Record<string, unknown>) => {
    setRunning(true);
    try {
      const data = await post<ParseResult>("/api/workbench/parse", {
        text: values.text,
        platform: values.platform ?? "onebot",
        channel_kind: values.channel_kind ?? "group",
        channel_id: values.channel_id ?? "0",
        role: values.role ?? 1,
        at_bot: values.at_bot ?? true,
      });
      setResult(data);
    } catch (exc) {
      message.error((exc as Error).message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <Flex vertical gap={16}>
      <Alert
        type="warning"
        showIcon
        message="这里是「预演」而不是「执行」"
        description="只做事件构造 + 规则匹配 + 参数解析，绝不调用插件 handler，也不会真的发消息。"
      />

      <Card size="small" title="发一句话试试谁接管">
        <Form
          layout="vertical"
          size="small"
          initialValues={{
            text: "",
            platform: "onebot",
            channel_kind: "group",
            channel_id: "200000001",
            role: 1,
            at_bot: true,
          }}
          onFinish={run}
        >
          <Row gutter={12}>
            <Col span={10}>
              <Form.Item name="text" label="消息文本" rules={[{ required: true, message: "写点什么" }]}>
                <Input placeholder="比如：画一只猫" />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="platform" label="平台">
                <Select
                  options={[
                    { value: "onebot", label: "onebot" },
                    { value: "official", label: "official" },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={4}>
              <Form.Item name="channel_kind" label="频道类型">
                <Select
                  options={[
                    { value: "group", label: "群" },
                    { value: "private", label: "私聊" },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={3}>
              <Form.Item name="channel_id" label="频道号">
                <Input />
              </Form.Item>
            </Col>
            <Col span={3}>
              <Form.Item name="role" label="身份">
                <Select
                  options={[
                    { value: 1, label: "群友" },
                    { value: 2, label: "管理员" },
                    { value: 3, label: "群主" },
                  ]}
                />
              </Form.Item>
            </Col>
          </Row>
          <Flex align="center" gap={16}>
            <Form.Item name="at_bot" label="@ 了机器人" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Switch size="small" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={running}>
              预演
            </Button>
          </Flex>
        </Form>
      </Card>

      {result ? (
        <>
          <Card size="small" title={`匹配到 ${result.matched.length} 条规则（共 ${result.rules_total} 条）`}>
            <Table<MatchedRule>
              size="small"
              rowKey={(r) => `${r.plugin}:${r.rule}`}
              pagination={false}
              dataSource={result.matched}
              locale={{ emptyText: "没有规则接管这条消息" }}
              columns={[
                { title: "插件", dataIndex: "plugin", width: 120 },
                { title: "规则", dataIndex: "rule", width: 180 },
                { title: "优先级", dataIndex: "priority", width: 80 },
                {
                  title: "命令",
                  dataIndex: "commands",
                  width: 160,
                  render: (v: string[]) => (v?.length ? v.join(" / ") : "—"),
                },
                {
                  title: "解析出的参数",
                  dataIndex: "params",
                  render: (v: Record<string, string>) => (
                    <Typography.Text style={{ fontFamily: "monospace", fontSize: 12 }}>
                      {Object.keys(v ?? {}).length === 0
                        ? "（无）"
                        : Object.entries(v)
                            .map(([k, val]) => `${k}=${val}`)
                            .join("  ")}
                    </Typography.Text>
                  ),
                },
                {
                  title: "阻断",
                  dataIndex: "block",
                  width: 70,
                  render: (v: boolean) => (v ? <Tag color="orange">是</Tag> : "—"),
                },
              ]}
            />
          </Card>

          <Card size="small" title={`未接管 ${result.skipped.length} 条`}>
            <Table<SkippedRule>
              size="small"
              rowKey={(r) => `${r.plugin}:${r.rule}`}
              pagination={{ pageSize: 12, hideOnSinglePage: true }}
              dataSource={result.skipped}
              columns={[
                { title: "插件", dataIndex: "plugin", width: 120 },
                { title: "规则", dataIndex: "rule", width: 200 },
                { title: "优先级", dataIndex: "priority", width: 80 },
                { title: "原因", dataIndex: "reason" },
              ]}
            />
          </Card>
        </>
      ) : null}
    </Flex>
  );
}