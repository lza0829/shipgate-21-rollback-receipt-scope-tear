# 发版闸（Shipgate）

骨架：发版申请入闸 → 环境晋升裁决 → 放行/回滚窗 → 发布结果对账

- 前端 http://127.0.0.1:6800
- API http://127.0.0.1:11800
- 数据库环境变量 `SHIPGATE_DB`（容器内默认 `/data/shipgate.db`）

```bash
docker compose up -d --build
```

## 回退时段写闸（硬闸，全局唯一口径）

选定「以回退时段真正卡写为准」：回退窗关闭时回退写口**整笔失败**，发版单、名册、实例
均不得进已回滚/切旧版，对账不得提前计成功；拒绝以独立事务留 `回退拒因` 审计痕迹并计入
对账「时段外回退尝试」桶（绝不只灰按钮）。读侧 `可回退` 与写侧卡闸共用同一谓词
`rollback_window_open`。写事务 `BEGIN IMMEDIATE` + 条件状态迁移，保证双回退只写出一个
目标版本；名册与实例在同一事务内整组收口，半截分叉可整笔回收到操作前。详见
[docs/rollback-gate.md](docs/rollback-gate.md)。金丝雀比例真源另立，不在本闸内。

后端夹具（18 例）：`test_rollback_gate.py` 10 例（时段关闭/开放回退、双回退、半截分叉、
整笔回滚、读写同判、审计过滤、健康探针、对账桶、开闭戳、空目标）；
`test_rollback_evidence.py` 8 例钉「假回滚成功收口」——成功回执须由写闸拒写旁读∪批次册正式行∪
回滚桶水位∪审计同命运重放四证复核、事件重放揭穿假回执、窗关硬拒水位不涨、回滚与放行写口交错收束、
并发单正式行、跨环窗旁读、在途回滚格、surface/rollback21 旁路毒根不复活。

```bash
pip install -r backend/requirements.txt pytest httpx
python -m pytest backend/tests -q
```
