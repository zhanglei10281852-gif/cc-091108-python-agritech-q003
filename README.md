# 县级植保作业台

以 Python 完整承接**田块申报 → 处方审签 → 航段执行 → 证据归档**，面向蜂场药害类投诉的可追溯场景。坐标 WGS84，剂量 ml/亩，距离米。

## 解决什么问题

蜂场举报药害时，原流程说不清三件事：飞手航迹是否真的避开了缓冲带、中途换药箱后实际用了哪批药、迟到的离线数据会不会把原结论改掉。本系统的回答方式：

1. **处方审签即冻结**。审核人、适用作物、药剂批次、剂量窗口、安全间隔（reentry）、风速/降雨阈值全部进入冻结快照哈希（`PRESCRIPTION_APPROVED.snapshot_hash`），任务只引用该哈希；已审签处方不可改、不可重复审签。
2. **作业条件越限自动处置**。风速或降雨越过阈值的观测一旦记录：未起飞航段自动 `INVALIDATED` 且禁止起飞；已起飞航段转入 `BREACHED_AIRBORNE`，必须先记录返航（`RETURN`）或带书面理由的继续（`CONTINUE`）才能降落。
3. **离线数据幂等同步、异常单列**。定位点带 `device_at`/`received_at`，可稍后批量补传；按 `point_id` 全局去重，重复同步只产生 `TRACK_POINT_REJECTED`，绝不重复计药。越界片段、缓冲带侵入、同批次内时间倒退、超标签剂量、批次不符、未知药箱六类发现分别呈现。
4. **封存后不改写，补遗独立**。任务结论封存（`seal_hash`）后到达的点自动路由为补遗：单独分析、单独哈希、引用原封存哈希，原结论哈希永不变；重复补遗无新点时被拒。
5. **补飞是独立证据链**。补飞使用新处方、新任务号，以 `parent_task_id` 指向原任务；投诉追溯双向可见。
6. **一次投诉追到全部要素**。`complaint_trace` 给出审核人与审签时间、每航段覆盖面积（亩）与状态、越界/侵入明细、到每个敏感区的最近距离与缓冲余量、换箱节点及换箱后批次、按实际批次汇总的用药量、封存/补遗哈希。

## 账本

所有事实只追加到一个 JSONL 哈希链（`crop_spray/ledger.py`）：每条事件含 `seq/prev_hash/hash`，对规范化 JSON 取 SHA-256。删除、插入、改写任何一条都会在重新打开时抛出「哈希链断裂 / 内容被篡改」。业务状态完全由重放事件得到（`crop_spray/service.py`，事件溯源）。

## 模块

| 文件 | 职责 |
|---|---|
| `crop_spray/geo.py` | 距离/面积、点在多边形内、缓冲圆、覆盖栅格（2 m，去重） |
| `crop_spray/models.py` | 枚举与数据模型（航段状态、发现分类） |
| `crop_spray/ledger.py` | 只追加哈希链账本 |
| `crop_spray/analysis.py` | 纯函数航迹分析：覆盖、越界、侵入、倒退、剂量、批次 |
| `crop_spray/service.py` | 申报/审签/任务/气象/同步/封存/补遗/投诉追溯 |
| `crop_spray/cli.py` | 植保员命令行 |
| `scripts/generate_samples.py` | 从资料生成两份离线航迹样例 |
| `scripts/demo.py` | 端到端演示（两个任务 + 投诉 + 补飞） |

## 资料

`reference/domain.json` 为田块（FIELD-204，水稻）、蜂场敏感区（APIARY-8，缓冲 120 m）、标签（CHEM-A17，18–25 ml/亩，安全间隔 24 h）。

- `reference/track_sample_a.json`：99 点正常航次，蛇形航线、TANK-01→TANK-02 换箱、离线批量补传后还有 39 点迟到。
- `reference/track_sample_b.json`：13 点问题航次，东出边界逼近蜂场（最近约 48 m，侵入 120 m 缓冲）、同批次内 1 处时间倒退、单点 40 ml/亩超标、返航时换上不在处方内的 BATCH-777，末 2 点封存后才同步。

样例可重新生成：

```bash
PYTHONPATH=. python3 scripts/generate_samples.py
```

## 使用

```bash
# 自检（含原有资料测试，共 28 个用例）
python3 -m unittest discover -s tests

# 端到端演示，产物在 runtime/（complaint_trace.json、summary_*.json、verification.json）
PYTHONPATH=. python3 -m scripts.demo

# 命令行
PYTHONPATH=. python3 -m crop_spray.cli sync --task TASK-B --leg B-L1 \
    --batch SYNC-B-1 --json reference/track_sample_b.json
PYTHONPATH=. python3 -m crop_spray.cli trace COMP-9
PYTHONPATH=. python3 -m crop_spray.cli export TASK-B   # 可校验摘要
PYTHONPATH=. python3 -m crop_spray.cli verify
```

## 导出摘要如何校验

`export` 输出 `ledger_head`（账本最后事件哈希）、`event_count`、`seal_hash`、`addendum_hashes`、`rx_snapshot_hash`。接收方用同一账本文件执行 `verify`：哈希链重算通过，且封存内容重哈希等于 `seal_hash`，即证明证据未被改动、迟到数据没有悄悄改写原结论。
