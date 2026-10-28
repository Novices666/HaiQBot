/**
 * 频道选择器：从「协议端真实群/好友 + 已配置 + 最近出现过」里搜，也允许直接手打群号。
 *
 * 为什么不用纯 Select：群号是**平台上的既成事实**，新拉的群、刚加的私聊不一定
 * 在候选里（协议端没连上、或者拿不到全量列表）。纯 Select 会把人卡死在半路，
 * 所以这里用能搜索的下拉 + 可自由输入，两者都要。
 */
import { useMemo } from "react";
import { AutoComplete, Tag, Typography } from "antd";
import type { ChannelCandidate } from "../types";

type Option = { value: string; label: string };

export default function ChannelPicker({
  candidates,
  prefix,
  value,
  onChange,
}: {
  candidates: ChannelCandidate[];
  /** `onebot:group:` 这样的前缀，用来只显示同类候选 */
  prefix: string;
  value?: string;
  onChange?: (next: string) => void;
}) {
  const options = useMemo<Option[]>(() => {
    const rows = candidates
      .filter((item) => `${item.platform}:${item.kind}:` === prefix)
      .map((item) => {
        const flags = [
          item.configured ? (item.enabled ? "已开启" : "已存在·停用") : "",
          item.sources.length > 1 ? item.sources.join("+") : item.sources[0] ?? "",
        ].filter(Boolean);
        return {
          value: item.id,
          label: `${item.label || item.id}${flags.length ? ` · ${flags.join(" · ")}` : ""}`,
        };
      });
    // 当前值不在候选里（刚手打过）也补进去，不然下拉看起来像"没选中"
    if (value && !rows.some((row) => row.value === value)) {
      rows.unshift({ value, label: `${value}（当前值，不在候选里）` });
    }
    return rows;
  }, [candidates, prefix, value]);

  return (
    <AutoComplete
      value={value}
      onChange={(next: string) => onChange?.(next)}
      options={options}
      style={{ width: 260 }}
      placeholder="搜群名或群号，也可直接手填"
      filterOption={(input, option) =>
        String(option?.label ?? "")
          .toLowerCase()
          .includes(input.toLowerCase()) ||
        String(option?.value ?? "").includes(input)
      }
      notFoundContent={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          候选里没有；直接输入群号/QQ 号即可
        </Typography.Text>
      }
    />
  );
}

/** 候选来源的一行小字说明，挂在输入框下面。 */
export function CandidateHint({ errors, total }: { errors: string[]; total: number }) {
  if (errors.length === 0) {
    return (
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {total > 0 ? `候选 ${total} 个：协议端群/好友 + 已配置 + 最近出现过` : "没有候选，直接手填"}
      </Typography.Text>
    );
  }
  return (
    <Typography.Text type="warning" style={{ fontSize: 12 }}>
      <Tag color="orange">候选不全</Tag>
      {errors.join("；")}（可直接手填群号）
    </Typography.Text>
  );
}
