# 移动广告合规实验室

归集多设备广告行为、无障碍操作证据、规则判定与整改复测，支撑对锁屏画报、开屏弹窗、
摇一摇广告的专项整治。

## 业务保证

- **三条操作轨迹并存**：正常用户（`normal`）、读屏用户（`screen_reader`）、老人模式
  （`elderly`）；同一构建在不同设备、不同轨迹上的证据分别留存，互不合并。
- **证据只增不改**：设备、构建、事件、规范版本只允许登记不允许覆盖。开发者提交新版
  得到新 `build_id`，旧构建证据原样保留；告知材料出具时对发现做快照，事后任何操作
  不改变材料内容。
- **采集幂等**：事件按 `(task_id, event_id)` 与「规范化内容指纹」双重判重。
  字段顺序变化不影响判重——规范化内容完全一致才列入 `duplicates`；编号相同而
  广告位、发生时间、事件类型或责任主体等内容不同时作为 `conflicts` 返回，保存
  首次与冲突两份摘要，**不覆盖首次证据、不进入任务事件索引、不触发重新判定，也
  不改动既有发现**。任务完成后的迟到新事件照常追加（标记 `late=true`）并触发
  追加判定，判定按指纹去重；但同号异内容冲突即使在任务完成后到达也只记冲突，
  不得伪装成迟到证据。冲突编号与差异字段在批次响应与任务报告中均可定位。
- **批次原子**：一个事件批次先整体校验再统一落地，任一条非法则整批拒绝，
  事件接收仓储与任务事件索引要么一致更新、要么都不动。批次中混有新事件、
  完全重放与冲突事件时，按 `results` 逐条报告 `accepted/duplicate/conflict`。
- **版本固化**：创建任务时固化当时生效的规范版本与最新脚本版本；规范或脚本更新只
  影响之后创建的任务，旧任务永远按原版本判定。
- **规则与复核分离**：自动规则只能产生 `suspected`（涉嫌）发现；复核员 `confirmed`
  后才建立整改案件，已确认且尚未告知的发现才能进入告知材料，整改期限取所依据各版
  规范中最短者。
- **复测不抹历史、回潮可累计**：复测通过只关闭当前整改周期，周期内问题时段
  （首末观测时间）永久保留；已整改后再次确认问题开启新周期，`relapse_count` 加一，
  承办人员可按责任主体看到历次周期、告知、复测（含失败记录）与期限。
- **责任区分**：每条发现记录责任主体（`app` 应用运营者 / `advertiser` 广告主 /
  `sdk` 嵌入SDK）与完整责任链。摇一摇/自动跳转优先归 SDK，其次广告主；关闭路径
  归应用运营者。

## 自动规则（仅标涉嫌）

| 规则 | 触发条件（依据任务固化的规范版本） |
| --- | --- |
| `R-CLOSE-001` | 广告期间无关闭入口；入口出现晚于 `close_max_delay_seconds`（默认 3s）；可点区域小于 44dp（老人模式 56dp）；读屏轨迹下入口不可聚焦或无操作标签 |
| `R-SHAKE-001` | 摇一摇触发跳转时，峰值加速度、旋转角度或读数持续时间任一低于规范下限（默认 15 m/s²、35°、3s） |
| `R-JUMP-001` | `trigger=auto` 的跳转，且跳转前无任何用户操作事件 |

## 运行

```bash
python3 service.py --check          # 基础自检
python3 service.py --port 8000      # 启动服务
LAB_DATA_FILE=lab.json python3 service.py   # 证据快照落盘，重启恢复
npm test                            # 运行契约 + 领域 + HTTP 共 36 项测试
```

## 接口一览

所有请求/响应均为 UTF-8 JSON；领域错误返回 `400`，实体不存在返回 `404`。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /admin/regulations` | 登记规范版本（`version`、`effective_at`、可选 `params`） |
| `POST /admin/scripts` | 登记测试脚本版本（`script_id`、`version`） |
| `POST /devices` | 登记设备（`device_id`、`model`、`os_version`） |
| `POST /builds` | 登记应用构建（`app_id`、`app_name`、`developer`、`version_code`） |
| `POST /tasks` | 创建采集任务（`build_id`、`device_id`、`track`），响应含固化版本 |
| `POST /tasks/{id}/events` | 幂等上报事件批次 `{"events": [...]}`，逐条返回 `results`（`accepted/duplicate/conflict`），并汇总 `accepted/duplicates/conflicts` |
| `POST /tasks/{id}/complete` | 完成采集并运行规则 |
| `GET /tasks/{id}` | 单任务报告：设备/系统/构建/无障碍设置/操作轨迹/发现与证据 |
| `GET /builds/{id}/report` | 同一构建跨设备、跨轨迹汇总 |
| `POST /findings/{id}/review` | 复核：`{"decision": "confirmed|dismissed", "reviewer"}` |
| `POST /subjects/{type}/{id}/notices` | 对已确认发现出具告知材料（期限、依据版本、证据快照） |
| `POST /subjects/{type}/{id}/retests` | 登记复测 `{"task_id": ...}`，通过则关闭当前周期 |
| `GET /subjects/{type}/{id}` | 承办人员视图：各整改周期、期限、问题时段、回潮次数 |

### 事件结构

```json
{
  "event_id": "端上唯一事件号（重传判重依据）",
  "seq": 1,
  "type": "ad_shown | close_affordance | sensor_reading | jump | network_response | gesture",
  "occurred_at": 1700000100,
  "payload": {
    "ad_id": "a1",
    "trigger": "auto | shake",
    "target_url": "https://...",
    "sdk": {"id": "shake-sdk-9", "name": "..."},
    "advertiser": {"id": "ad-brand-x"},
    "visible_after_seconds": 5,
    "touch_target_dp": 36,
    "screen_reader_actionable": false,
    "peak_acceleration": 8,
    "peak_rotation_deg": 10,
    "reading_seconds": 1
  }
}
```

### 批次上报响应（幂等边界）

指纹只覆盖 `seq/type/occurred_at/payload`（`received_at`、`late` 为服务端状态，
不参与比对），JSON 键经排序规范化，故字段顺序变化仍判重。

```json
{
  "accepted": ["e-new-1"],
  "duplicates": ["e-a1-shown"],
  "conflicts": [
    {
      "event_id": "e-a1-shown",
      "differing_fields": ["payload.advertiser.id", "payload.placement"],
      "first": {"seq": 1, "type": "ad_shown", "occurred_at": 1700000100,
                "payload": {"ad_id": "a1", "placement": "splash"}},
      "conflicting": {"seq": 1, "type": "ad_shown", "occurred_at": 1700000100,
                      "payload": {"ad_id": "a1", "placement": "lockscreen"}},
      "conflict_count": 1,
      "first_received_at": 1700000200,
      "latest_conflict_at": 1700000300
    }
  ],
  "results": [
    {"event_id": "e-new-1", "status": "accepted"},
    {"event_id": "e-a1-shown", "status": "duplicate"},
    {"event_id": "e-a1-shown", "status": "conflict",
     "differing_fields": ["payload.advertiser.id", "payload.placement"]}
  ],
  "late_arrivals": false
}
```

冲突不改变 HTTP 状态（整批仍为 `202`，逐条见 `results`），也不覆盖首次证据；
任务报告 `GET /tasks/{id}` 的 `conflicts` 段同样可定位冲突编号与差异字段。

## 模块

- `domain.py`：领域模型与规则引擎（纯 Python，无框架依赖），含快照序列化。
- `service.py`：HTTP 入口与持久化仓储（`Store`，线程安全、原子写盘）。
- `test_domain.py` / `test_api.py`：领域规则与 HTTP 全链路测试。
- `fixtures/domain.json`：领域名词与状态词表，供接口联调对齐语义。

## 测试与构建

测试命令：

```bash
npm test
```

编译或构建命令：

```bash
python3 -m compileall -q .
```

上述命令用于本地核对服务代码，不需要另行启动外部基础设施。
