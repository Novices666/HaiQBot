/** 总览：一眼看清「活着吗、谁在跑、最近发生了什么」。4 秒轮询。 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  Row,
  Statistic,
  Table,
  Tag,
  Tooltip as AntTooltip,
  Typography,
  theme,
} from "antd";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmtDuration, fmtTime, text, usePoll } from "../lib";
import type { AuditRow, Overview } from "../types";

type Sample = { t: string; events: number; handled: number };

/** 四张统计卡共用同一套样式：原来「运行时长」单写成 20px、其余用默认，看着像错版。
 *  字号不再自己定，交回 antd 默认（24）—— 我只统一「对齐」这一件事。 */
const STAT_STYLE = { fontVariantNumeric: "tabular-nums" as const };

export default function OverviewPage() {
  const { token } = theme.useToken();
  const { data, error } = usePoll<Overview>("/api/overview", 4000);
  const [series, setSeries] = useState<Sample[]>([]);
  const lastSample = useRef<Overview | null>(null);

  // 只有事件计数变化才落一个采样点，避免同一数值重复入栈
  const events = data?.runtime.events_total ?? 0;
  const handled = data?.runtime.handled_total ?? 0;
  useEffect(() => {
    if (!data || data === lastSample.current) return;
    lastSample.current = data;
    setSeries((prev) =>
      [...prev, { t: new Date().toLocaleTimeString("zh-CN", { hour12: false }), events, handled }].slice(
        -40,
      ),
    );
  }, [data, events, handled]);

  const online = useMemo(
    () => Object.values(data?.platforms ?? {}).filter((p) => p.health?.connected === true).length,
    [data],
  );
  const platformCount = Object.keys(data?.platforms ?? {}).length;

  return (
    <Flex vertical gap={16}>
      {error ? <Alert type="error" showIcon message={error.message} description={error.hint} /> : null}

      <Row gutter={[16, 16]}>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Statistic
              title="运行时长"
              value={fmtDuration(data?.runtime.uptime_seconds)}
              valueStyle={{ ...STAT_STYLE, whiteSpace: "nowrap" }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Statistic title="累计事件" value={data?.runtime.events_total ?? 0} valueStyle={STAT_STYLE} />
          </Card>
        </Col>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Statistic title="已处理" value={data?.runtime.handled_total ?? 0} valueStyle={STAT_STYLE} />
          </Card>
        </Col>
        <Col xs={12} sm={12} lg={6}>
          <Card>
            <Statistic
              title="平台在线"
              value={online}
              suffix={`/ ${platformCount}`}
              // 数字是文字，用 *Text 版本（colorSuccess/colorError 是给图标和边框的，压白底只有 2.2–3.3:1）
              valueStyle={{ ...STAT_STYLE, color: online > 0 ? token.colorSuccessText : token.colorErrorText }}
            />
          </Card>
        </Col>
      </Row>

      <Card title="事件吞吐（本页面采样）" size="small">
        <div style={{ height: 200 }}>
          {series.length < 2 ? (
            <Flex align="center" justify="center" style={{ height: "100%" }}>
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="等两次采样…" />
            </Flex>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={series} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
                <defs>
                  <linearGradient id="event-area-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={token.colorPrimary} stopOpacity={0.32} />
                    <stop offset="100%" stopColor={token.colorPrimary} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke={token.colorBorderSecondary} />
                {/* 轴标签 12px：11px 在高分屏上已经糊成一团 */}
                <XAxis dataKey="t" fontSize={12} tickLine={false} stroke={token.colorTextSecondary} />
                <YAxis fontSize={12} tickLine={false} allowDecimals={false} stroke={token.colorTextSecondary} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                <Area
                  type="monotone"
                  dataKey="events"
                  name="事件总数"
                  stroke={token.colorPrimary}
                  strokeWidth={2}
                  fill="url(#event-area-fill)"
                />
                <Area
                  type="monotone"
                  dataKey="handled"
                  name="已处理"
                  stroke={token.colorSuccessText}
                  strokeWidth={1.5}
                  fillOpacity={0}
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="平台连接" size="small">
            {platformCount === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有启用的平台适配器" />
            ) : (
              <Descriptions size="small" column={1} bordered>
                {Object.entries(data?.platforms ?? {}).map(([name, info]) => (
                  <Descriptions.Item key={name} label={name}>
                    <Flex gap={8} align="center">
                      <Tag color={info.health?.connected ? "green" : "red"}>
                        {info.health?.connected ? "已连接" : "未连接"}
                      </Tag>
                      <Typography.Text type="secondary">{text(info.bot_id)}</Typography.Text>
                    </Flex>
                  </Descriptions.Item>
                ))}
              </Descriptions>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="运行参数" size="small">
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="OneBot 模式">
                {text(data?.config.onebot_mode)} {data?.config.onebot_enabled ? "（启用）" : "（停用）"}
              </Descriptions.Item>
              <Descriptions.Item label="QQ 官方通道">
                {data?.config.official_enabled ? "启用" : "停用"}
              </Descriptions.Item>
              <Descriptions.Item label="配额控制">
                {data?.config.quota_enabled ? "启用" : "停用"}
              </Descriptions.Item>
              <Descriptions.Item label="出站节流">
                {data?.outbound
                  ? `${text(data.outbound.min_interval_seconds)} 秒/条 · ${text(
                      data.outbound.global_per_minute,
                    )} 条/分`
                  : "—"}
              </Descriptions.Item>
              <Descriptions.Item label="媒体库">{data?.media.count ?? 0} 项</Descriptions.Item>
              <Descriptions.Item label="面板写入">
                {data?.config.write_enabled ? <Tag color="green">可写</Tag> : <Tag>只读</Tag>}
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <Card title="最近审计" size="small">
        <Table<AuditRow>
          scroll={{ x: "max-content" }}
          size="small"
          // 审计行没有业务主键：用时间戳 + 序号，避免纯索引在插入时错位
          rowKey={(row, index) => `${row.ts}-${index}`}
          pagination={false}
          dataSource={data?.recent_audit ?? []}
          locale={{ emptyText: "还没有写操作记录" }}
          columns={[
            { title: "时间", dataIndex: "ts", width: 168, render: (v: number) => fmtTime(v) },
            { title: "动作", dataIndex: "action", width: 150 },
            { title: "插件", dataIndex: "plugin_id", width: 80, render: (v: unknown) => text(v) },
            {
              title: "详情",
              dataIndex: "detail",
              ellipsis: { showTitle: false },
              render: (v: unknown) => (
                <AntTooltip title={text(v)}>
                  <Typography.Text type="secondary" style={{ fontFamily: "monospace", fontSize: 12 }}>
                    {text(v)}
                  </Typography.Text>
                </AntTooltip>
              ),
            },
          ]}
        />
      </Card>
    </Flex>
  );
}
