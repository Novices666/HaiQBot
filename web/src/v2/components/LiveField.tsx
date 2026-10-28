/**
 * 动态可选值控件：值要现问接口（模型列表就是这种）。
 *
 * 三种状态，一个都不许静默：
 * 1. 拉取中 —— 显示 loading 的 Select，不是空白；
 * 2. 拉到了 —— 可搜索下拉，接口没返回当前值时把当前值补进列表（否则会被清空）；
 * 3. 拉不到 —— 退回手填输入框 + 红字说明原因（配置页不能因为网关卡住就没法改）。
 *
 * 依赖 `field.dynamic_inputs`：把**还没保存**的那几个值（比如刚改的 base_url）
 * 一起传给后端，省掉「先保存才能拉列表」这种反人类步骤。
 */
import { useCallback, useEffect, useState } from "react";
import { Button, Flex, Input, Select, Space, Tooltip, Typography } from "antd";
import { get } from "../../api";
import type { FieldOptions, SchemaField } from "../types";

type Option = { value: string; label: string };

export default function LiveField({
  pluginId,
  field,
  collect,
  value,
  onChange,
}: {
  pluginId: string;
  field: SchemaField;
  /** 取当前表单值（可能还没保存）——调用方必须给稳定引用，否则会反复拉取。 */
  collect: () => Record<string, unknown>;
  value?: string;
  onChange?: (next: string) => void;
}) {
  const [options, setOptions] = useState<Option[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const inputs = field.dynamic_inputs ?? [];

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const values = collect() ?? {};
      const params = new URLSearchParams();
      for (const name of inputs) {
        const raw = values[name];
        if (raw !== undefined && raw !== null && raw !== "") params.set(name, String(raw));
      }
      const query = params.toString();
      const path = `/api/plugins/${encodeURIComponent(pluginId)}/options/${encodeURIComponent(field.key)}`;
      const data = await get<FieldOptions>(query ? `${path}?${query}` : path);
      const list = data?.options ?? [];
      setOptions(list);
      if (data?.error) setError(data.error);
      else if (list.length === 0) setError("接口没有返回任何可选值");
    } catch (exc) {
      setOptions([]);
      setError((exc as Error).message);
    } finally {
      setLoading(false);
    }
  }, [collect, field.key, pluginId, inputs.join(",")]);

  useEffect(() => {
    void load();
  }, [load]);

  const retry = (
    <Tooltip title="重新从接口拉取">
      <Button onClick={() => void load()}>刷新</Button>
    </Tooltip>
  );

  if (loading) {
    return <Select style={{ width: "100%" }} loading disabled placeholder="正在从接口拉取…" />;
  }

  if (options.length === 0) {
    return (
      <Flex vertical gap={4}>
        <Space.Compact style={{ width: "100%" }}>
          <Input
            value={value}
            placeholder={field.default === undefined ? "" : String(field.default)}
            onChange={(event) => onChange?.(event.target.value)}
          />
          {retry}
        </Space.Compact>
        <Typography.Text type="danger" style={{ fontSize: 11 }}>
          {error ?? "拉取失败"}（可直接手填，或改完上面的地址再点刷新）
        </Typography.Text>
      </Flex>
    );
  }

  // 接口没返回当前值（模型下线了 / 换了网关）时补一行，别让保存把原值冲掉
  const missing = value && !options.some((item) => item.value === value);
  const merged: Option[] = missing
    ? [{ value: value as string, label: `${value}（当前值，接口未返回）` }, ...options]
    : options;

  return (
    <Flex vertical gap={2}>
      <Flex gap={6}>
        <Select
          showSearch
          allowClear
          style={{ flex: 1 }}
          value={value}
          onChange={(next: string) => onChange?.(next)}
          options={merged}
          optionFilterProp="label"
          placeholder="搜索或选择"
        />
        {retry}
      </Flex>
      <Typography.Text type={error ? "danger" : "secondary"} style={{ fontSize: 11 }}>
        {error ? `${error}（下面是上次拉到的列表）` : `接口返回 ${options.length} 个可选值`}
      </Typography.Text>
    </Flex>
  );
}
